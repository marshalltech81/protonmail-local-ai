"""Build the retrieval baseline with a real embedding model (#1439).

Stage 1 of #1425: the synthetic baseline corpus (``corpus.py``) and its
golden questions are embedded by the operator's OpenAI-compatible
embedder instead of the hashed one, so the retrieval floors in
``golden.json`` ``real_embedder_floors`` measure semantic recall. It is
synthetic regression evidence, not a claim about any mailbox's quality.
Nothing but the synthetic corpus and questions is sent; no index or
Maildir other than the one this build writes is read.

The build runs ``build.build`` once per repeat (``--repeats``, at least
two) into ``<out_dir>/repeat-<N>``, each with its own provider vectors,
and writes ``<out_dir>/run.json``: the requests sent, the input tokens
the provider reported, the wall time, the cache hits and misses, and
the repeat-to-repeat variation (the largest and mean cosine distance
between the vectors two repeats got for the same text). Step 2,
``mcp-server/tests/baseline/test_real_embedder_baseline.py``, ranks the
golden questions on every repeat, checks the floors and reports ranking
flips between repeats.

Vectors are cached in ``<cache_dir>/vectors.sqlite`` (git-ignored,
mode 600) under a key of the text and the whole request it was sent
in, the repeat number and the embedding identity (endpoint, a digest
of the whole base URL, model, batch size, dimensions, SDK and
encoding), so a rerun with an unchanged
corpus and configuration sends nothing, and a change re-sends whole
every request it changed. Delete the cache to measure the variation
afresh.

Every provider request, retries and the calibration request included,
counts against ``--max-requests``. The request that would exceed it is
not sent: the run stops and exits ``EXIT_INCONCLUSIVE`` (3), and no
floor is checked, so a capped run is never a pass.

Configuration comes from the environment, as for the indexer:
``EMBED_BASE_URL`` (a URL or ``default``), ``EMBED_MODEL`` and the
optional ``EMBED_MODE`` (``openai`` only). The key is read inside this
process from ``<secrets_dir>/embed_api_key.txt``, which must be mode 600
and non-empty; it is never a command argument and never printed.

Usage, from ``indexer/`` (``make baseline-real-embedder`` runs both
steps):

    uv run python -m tests.baseline.real_embedder <out_dir> <golden.json> \\
        --cache-dir <dir> --secrets-dir <dir> [--repeats N] \\
        [--max-requests N] [--batch-size N]
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import time
import urllib.parse
from array import array
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openai
from src import main
from src.database import EMBEDDING_DIM
from src.embed_identity import cosine_distance, sanitize_endpoint, verify_or_record_embedder
from src.embedder import (
    EmbedResponseError,
    OpenAIEmbedder,
    _is_transient_embed_error,
    embed_retry_after_seconds,
    scrub_embed_error,
)

from tests.baseline.build import build

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INCONCLUSIVE = 3
KEY_FILE = "embed_api_key.txt"
CACHE_FILE = "vectors.sqlite"


class UsageError(ValueError):
    """One or more arguments failed the table; the message lists each."""


class EmbedBudgetExhausted(BaseException):
    """The request cap is reached; the next request was not sent.

    A ``BaseException`` so the indexer's ``except Exception`` handlers
    (outage probes, per-message fallbacks) cannot absorb it into a
    deferred job: the run stops at once and reports inconclusive.
    """


@dataclass(frozen=True)
class Argument:
    """One row of the argument table.

    ``source`` is ``positional``, ``flag`` (a ``--name`` option) or
    ``env``. ``kind`` names the check: ``int`` (whole number in
    ``minimum``..``maximum``), ``text`` (non-empty), ``choice`` (one of
    ``choices``, case-insensitive), ``url`` (a URL or ``default``),
    ``new_dir`` (absent or empty), ``file`` (an existing file),
    ``cache_dir`` (absent or a directory) and ``secrets_dir`` (holds a
    mode-600, non-empty ``embed_api_key.txt``). ``default`` applies
    when the value is missing; a row without one is required.
    """

    name: str
    source: str
    kind: str
    minimum: int | None = None
    maximum: int | None = None
    default: str | None = None
    choices: tuple[str, ...] = ()


# The one table every argument is checked against, before any work.
# No two arguments exclude or require each other.
ARGUMENTS: tuple[Argument, ...] = (
    Argument("out_dir", "positional", "new_dir"),
    Argument("golden", "positional", "file"),
    Argument("--cache-dir", "flag", "cache_dir"),
    Argument("--secrets-dir", "flag", "secrets_dir"),
    # At least two: the variation is measured between repeats.
    Argument("--repeats", "flag", "int", minimum=2, maximum=5, default="2"),
    # One repeat is about 100 requests at the default batch size: seven
    # chunk batches, one calibration and one per query (92, each sent
    # alone as mcp-server sends a search query).
    Argument("--max-requests", "flag", "int", minimum=1, maximum=1000, default="250"),
    # The indexer's own default (``EMBED_BATCH_SIZE``).
    Argument("--batch-size", "flag", "int", minimum=1, maximum=2048, default="64"),
    Argument("EMBED_BASE_URL", "env", "url"),
    Argument("EMBED_MODEL", "env", "text"),
    Argument("EMBED_MODE", "env", "choice", default="openai", choices=("openai",)),
)


@dataclass(frozen=True)
class Settings:
    out_dir: Path
    golden: Path
    cache_dir: Path
    repeats: int
    max_requests: int
    batch_size: int
    base_url: str
    model: str
    api_key: str = field(repr=False)


_WHOLE_NUMBER = re.compile(r"[0-9]+")


def _check(arg: Argument, raw: str | None, errors: list[str]) -> Any:
    """The checked value of one argument, or ``None`` after appending
    a fixed-text error naming it. Values are never echoed: the URL could
    carry credentials."""
    value = raw.strip() if raw is not None else ""
    if not value:
        if arg.default is None:
            errors.append(f"{arg.name} is required")
            return None
        value = arg.default
    match arg.kind:
        case "int":
            if not _WHOLE_NUMBER.fullmatch(value):
                errors.append(f"{arg.name} must be a whole number")
                return None
            number = int(value)
            assert arg.minimum is not None and arg.maximum is not None
            if not arg.minimum <= number <= arg.maximum:
                errors.append(f"{arg.name} must be between {arg.minimum} and {arg.maximum}")
                return None
            return number
        case "text":
            return value
        case "choice":
            if value.lower() not in arg.choices:
                errors.append(f"{arg.name} must be one of: {', '.join(arg.choices)}")
                return None
            return value.lower()
        case "url":
            try:
                url = main._resolve_base_url(arg.name, value, main._OPENAI_DEFAULT_URL)
            except ValueError as exc:
                errors.append(str(exc))
                return None
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                errors.append(f"{arg.name} must be an http(s) URL or `default`")
                return None
            if "@" in parts.netloc:
                errors.append(f"{arg.name} must not embed credentials (user:pass@host)")
                return None
            return url
        case "new_dir":
            path = Path(value)
            if path.exists() and (not path.is_dir() or any(path.iterdir())):
                errors.append(f"{arg.name} must not exist or be an empty directory")
                return None
            return path
        case "file":
            path = Path(value)
            if not path.is_file():
                errors.append(f"{arg.name} must be an existing file")
                return None
            return path
        case "cache_dir":
            path = Path(value)
            if path.exists() and not path.is_dir():
                errors.append(f"{arg.name} must be a directory")
                return None
            return path
        case "secrets_dir":
            key_path = Path(value) / KEY_FILE
            if not key_path.is_file():
                errors.append(f"{arg.name} must hold {KEY_FILE}")
                return None
            if stat.S_IMODE(key_path.stat().st_mode) & 0o077:
                errors.append(f"{KEY_FILE} must be mode 600")
                return None
            key = key_path.read_text(encoding="utf-8").strip()
            if not key:
                errors.append(f"{KEY_FILE} must not be empty")
                return None
            return key
    raise AssertionError(f"unknown argument kind {arg.kind}")


def validate(raw: Mapping[str, str | None]) -> Settings:
    """Check every argument in ``raw`` (keyed by ``Argument.name``)
    against ``ARGUMENTS``; raise ``UsageError`` listing every failure."""
    errors: list[str] = []
    values = {arg.name: _check(arg, raw.get(arg.name), errors) for arg in ARGUMENTS}
    if errors:
        raise UsageError("; ".join(errors))
    return Settings(
        out_dir=values["out_dir"],
        golden=values["golden"],
        cache_dir=values["--cache-dir"],
        repeats=values["--repeats"],
        max_requests=values["--max-requests"],
        batch_size=values["--batch-size"],
        base_url=values["EMBED_BASE_URL"],
        model=values["EMBED_MODEL"],
        api_key=values["--secrets-dir"],
    )


def parse(argv: list[str], env: Mapping[str, str]) -> Settings:
    """Collect the raw strings (argparse checks only the shape) and
    validate them all through ``validate``."""
    parser = argparse.ArgumentParser(prog="python -m tests.baseline.real_embedder")
    for arg in ARGUMENTS:
        if arg.source == "positional":
            parser.add_argument(arg.name)
        elif arg.source == "flag":
            parser.add_argument(arg.name, dest=arg.name)
    ns = vars(parser.parse_args(argv))
    raw: dict[str, str | None] = {
        arg.name: env.get(arg.name) if arg.source == "env" else ns.get(arg.name)
        for arg in ARGUMENTS
    }
    return validate(raw)


class RequestMeter:
    """Counts every embeddings request and refuses the one past the cap.

    It wraps the SDK's ``embeddings.create``, which the embedder calls
    once per attempt, so retries count too.
    """

    def __init__(self, max_requests: int) -> None:
        self.max_requests = max_requests
        self.requests = 0
        self.input_tokens = 0
        self.unreported = 0
        self.retries = 0

    def wrap(self, create: Callable[..., Any]) -> Callable[..., Any]:
        def metered(*args: Any, **kwargs: Any) -> Any:
            if self.requests >= self.max_requests:
                raise EmbedBudgetExhausted
            self.requests += 1
            try:
                response = create(*args, **kwargs)
            except BaseException:
                # It may have been processed (and billed) without
                # returning usage, so the token total is incomplete.
                self.unreported += 1
                raise
            tokens = getattr(getattr(response, "usage", None), "prompt_tokens", None)
            if isinstance(tokens, int) and not isinstance(tokens, bool):
                self.input_tokens += tokens
            else:
                self.unreported += 1
            return response

        return metered

    def attach(self, embedder: OpenAIEmbedder) -> None:
        embeddings = embedder.client.embeddings
        embeddings.create = self.wrap(embeddings.create)  # type: ignore[method-assign]


def embedding_identity(settings: Settings, role: str) -> dict[str, object]:
    """Everything besides the text and repeat that decides a vector.

    ``role`` is ``chunk`` (the indexer's batched, L2-normalised request)
    or ``query`` (one string per request, unnormalised, as mcp-server's
    ``EmbedClient.embed`` sends a search query).
    """
    shape: dict[str, object] = (
        {"input": "list", "batch_size": settings.batch_size, "normalization": "l2"}
        if role == "chunk"
        else {"input": "string", "batch_size": 1, "normalization": "none"}
    )
    return {
        "role": role,
        "endpoint": sanitize_endpoint(settings.base_url),
        # The sanitised endpoint drops a query string, which can select
        # another deployment; the digest keeps the whole URL apart
        # without holding it.
        "base_url_sha256": hashlib.sha256(settings.base_url.encode("utf-8")).hexdigest(),
        "model": settings.model,
        "dimensions": EMBEDDING_DIM,
        # The SDK asks for base64 (float32) when no format is given.
        "encoding_format": "sdk-default",
        "openai_sdk": openai.__version__,
        **shape,
    }


class VectorCache:
    """Vectors keyed by sha256(identity, repeat, item) in SQLite, where
    the item names a text in the request it was sent in
    (``CachedEmbedder.item``)."""

    def __init__(self, cache_dir: Path, identity: Mapping[str, object]) -> None:
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = cache_dir / CACHE_FILE
        self._conn = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL)"
        )
        self._identity = json.dumps(identity, sort_keys=True)

    def key(self, repeat: int, item: str) -> str:
        payload = json.dumps([self._identity, repeat, item])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> list[float] | None:
        row = self._conn.execute("SELECT vector FROM vectors WHERE key = ?", (key,)).fetchone()
        return None if row is None else array("d", row[0]).tolist()

    def put_many(self, items: list[tuple[str, list[float]]]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO vectors (key, vector) VALUES (?, ?)",
                [(key, array("d", vector).tobytes()) for key, vector in items],
            )

    def close(self) -> None:
        self._conn.close()


Sender = Callable[..., list[list[float]]]


# Attempts per query request, the first included, as the indexer's
# ``_EMBED_ATTEMPTS``; the wait before a retry is the provider's
# Retry-After, else 2, 4 s, capped at ``_MAX_RETRY_WAIT_SECS``.
_QUERY_ATTEMPTS = 3
_MAX_RETRY_WAIT_SECS = 30.0


def query_sender(
    embedder: OpenAIEmbedder,
    meter: RequestMeter,
    sleep: Callable[[float], None] = time.sleep,
) -> Sender:
    """Embed each text in its own request with a string ``input`` and
    return the provider's vector unnormalised: the request mcp-server's
    ``EmbedClient.embed`` sends for a search query, through the same
    (metered) SDK client. A transient failure (the indexer's
    classification: connection, timeout, 408, 429, 5xx) is retried;
    each retry counts in ``meter.retries`` and against the cap."""

    def create(text: str) -> Any:
        for attempt in range(1, _QUERY_ATTEMPTS + 1):
            try:
                return embedder.client.embeddings.create(model=embedder.model, input=text)
            except Exception as exc:
                if attempt == _QUERY_ATTEMPTS or not _is_transient_embed_error(exc):
                    raise
                meter.retries += 1
                wait = embed_retry_after_seconds(exc)
                sleep(min(_MAX_RETRY_WAIT_SECS, 2.0**attempt if wait is None else wait))
        raise AssertionError("unreachable")

    def send(
        texts: list[str], *, on_batch_complete: Callable[[], None] | None = None
    ) -> list[list[float]]:
        vectors = []
        for text in texts:
            response = create(text)
            if len(response.data) != 1:
                raise EmbedResponseError(
                    f"embedder returned {len(response.data)} vectors for 1 input"
                )
            vector = list(response.data[0].embedding)
            # The checks mcp-server's ``embed_query`` makes before a
            # query vector reaches search.
            if len(vector) != EMBEDDING_DIM:
                raise EmbedResponseError(
                    f"embedder returned a {len(vector)}-dim vector; the index has {EMBEDDING_DIM}"
                )
            if not all(math.isfinite(x) for x in vector):
                raise EmbedResponseError("embedder returned non-finite vector values")
            if not any(vector):
                raise EmbedResponseError("embedder returned an all-zero vector")
            vectors.append(vector)
        if on_batch_complete is not None:
            on_batch_complete()
        return vectors

    return send


class CachedEmbedder:
    """``EmbeddingBackend`` serving one repeat's vectors from the cache
    and passing the misses, in their original order, to ``send`` (the
    indexer's ``OpenAIEmbedder.embed_batch`` for chunks, ``query_sender``
    for queries).

    The provider's vector for a text can depend on the texts batched
    with it, so a text is cached under the whole call it came in (every
    text, in order) and its position: a call that differs in any text
    misses entirely and is sent whole, as a fresh build would send it.
    With ``one_at_a_time`` each text is its own request, so the call is
    the text alone.
    """

    def __init__(
        self, send: Sender, cache: VectorCache, repeat: int, *, one_at_a_time: bool = False
    ) -> None:
        # ``one_at_a_time`` sends and caches each miss on its own, so a
        # run stopped part-way keeps every vector it paid for.
        self.send = send
        self.one_at_a_time = one_at_a_time
        self.cache = cache
        self.repeat = repeat
        self.items: set[str] = set()
        self.hits = 0
        self.misses = 0

    def wait_for_ready(self, timeout: int = 120) -> None:
        return None

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    @staticmethod
    def item(call: list[str], index: int) -> str:
        digest = hashlib.sha256(json.dumps(call).encode("utf-8")).hexdigest()
        return f"{digest}:{index}"

    def embed_batch(
        self,
        texts: list[str],
        *,
        on_batch_complete: Callable[[], None] | None = None,
    ) -> list[list[float]]:
        items = [
            self.item([t], 0) if self.one_at_a_time else self.item(texts, i)
            for i, t in enumerate(texts)
        ]
        keys = [self.cache.key(self.repeat, item) for item in items]
        found = [self.cache.get(k) for k in keys]
        missing = [i for i, vector in enumerate(found) if vector is None]
        if missing and not self.one_at_a_time:
            # Any miss re-sends the whole call, so every text in it gets
            # the batch it would get in a fresh build.
            missing = list(range(len(texts)))
        self.hits += len(texts) - len(missing)
        self.misses += len(missing)
        groups = [[i] for i in missing] if self.one_at_a_time else [missing] if missing else []
        for group in groups:
            fresh = self.send([texts[i] for i in group], on_batch_complete=on_batch_complete)
            self.cache.put_many([(keys[i], v) for i, v in zip(group, fresh, strict=True)])
            for i, vector in zip(group, fresh, strict=True):
                found[i] = vector
        if not missing and on_batch_complete is not None:
            on_batch_complete()
        self.items.update(items)
        return [vector for vector in found if vector is not None]


def measure_variation(cache: VectorCache, items_per_repeat: list[set[str]]) -> dict[str, object]:
    """Cosine distance between repeat 1's vector and every later
    repeat's for each item (a text in its request) both embedded."""
    distances: list[float] = []
    differing = 0
    for repeat, items in enumerate(items_per_repeat[1:], start=2):
        for item in sorted(items_per_repeat[0] & items):
            first = cache.get(cache.key(1, item))
            later = cache.get(cache.key(repeat, item))
            assert first is not None and later is not None
            differing += first != later
            # Clamped: identical vectors can come out a rounding error
            # below zero.
            distances.append(max(0.0, cosine_distance(first, later)))
    return {
        "pairs": len(distances),
        "max_cosine_distance": max(distances, default=0.0),
        "mean_cosine_distance": sum(distances) / len(distances) if distances else 0.0,
        "pairs_not_identical": differing,
    }


def run(settings: Settings, meter: RequestMeter) -> dict[str, object]:
    """Build every repeat and return the run report (also written to
    ``<out_dir>/run.json``)."""
    started = time.monotonic()
    embedder = OpenAIEmbedder(
        settings.base_url,
        settings.model,
        api_key=settings.api_key,
        batch_size=settings.batch_size,
    )
    meter.attach(embedder)
    caches = {
        role: VectorCache(settings.cache_dir, embedding_identity(settings, role))
        for role in ("chunk", "query")
    }
    senders = {"chunk": embedder.embed_batch, "query": query_sender(embedder, meter)}
    items_per_repeat: dict[str, list[set[str]]] = {role: [] for role in caches}
    hits = misses = 0
    try:
        for repeat in range(1, settings.repeats + 1):
            cached = CachedEmbedder(senders["chunk"], caches["chunk"], repeat)
            queries = CachedEmbedder(senders["query"], caches["query"], repeat, one_at_a_time=True)

            def record_identity(db: Any, cached: CachedEmbedder = cached) -> None:
                verify_or_record_embedder(
                    db,
                    cached,
                    provider="openai",
                    endpoint=embedder.base_url,
                    model=settings.model,
                    dimensions=EMBEDDING_DIM,
                )

            build(
                settings.out_dir / f"repeat-{repeat}",
                settings.golden,
                embedder=cached,
                query_embedder=queries,
                record_identity=record_identity,
            )
            for role, used in (("chunk", cached), ("query", queries)):
                items_per_repeat[role].append(used.items)
                hits += used.hits
                misses += used.misses
        variation = {
            name: measure_variation(caches[role], items_per_repeat[role])
            for name, role in (("chunks", "chunk"), ("queries", "query"))
        }
    finally:
        for cache in caches.values():
            cache.close()
        embedder.client.close()
    report: dict[str, object] = {
        "endpoint": sanitize_endpoint(settings.base_url),
        "model": settings.model,
        "repeats": settings.repeats,
        "batch_size": settings.batch_size,
        "max_requests": settings.max_requests,
        "requests": meter.requests,
        "query_retries": meter.retries,
        "input_tokens": meter.input_tokens if meter.unreported == 0 else None,
        "wall_seconds": round(time.monotonic() - started, 1),
        "cache_hits": hits,
        "cache_misses": misses,
        "variation": variation,
    }
    (settings.out_dir / "run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _spend(meter: RequestMeter, started: float) -> str:
    tokens = (
        f"{meter.input_tokens} input tokens"
        if meter.unreported == 0
        else f"{meter.input_tokens} input tokens reported ({meter.unreported} requests without usage)"
    )
    return (
        f"{meter.requests} requests ({meter.retries} query retries), {tokens},"
        f" {time.monotonic() - started:.1f} s"
    )


def main_cli(argv: list[str], env: Mapping[str, str]) -> int:
    started = time.monotonic()
    try:
        settings = parse(argv, env)
    except UsageError as exc:
        print(f"real-embedder baseline: {exc}", file=sys.stderr)
        return EXIT_USAGE
    meter = RequestMeter(settings.max_requests)
    try:
        report = run(settings, meter)
    except EmbedBudgetExhausted:
        print(
            f"real-embedder baseline: INCONCLUSIVE: the request cap ({settings.max_requests})"
            f" was reached before the build finished; no floor was checked"
            f" ({_spend(meter, started)})",
            file=sys.stderr,
        )
        return EXIT_INCONCLUSIVE
    except Exception as exc:
        # The build's own RuntimeErrors are fixed text with counts;
        # anything else is rendered as the indexer renders embed errors.
        detail = str(exc) if type(exc) is RuntimeError else scrub_embed_error(exc)
        print(
            f"real-embedder baseline: FAILED: {detail} ({_spend(meter, started)})",
            file=sys.stderr,
        )
        return EXIT_FAILED
    variation = report["variation"]
    assert isinstance(variation, dict)
    print(
        f"real-embedder baseline: built {settings.repeats} repeats with {settings.model}:"
        f" {_spend(meter, started)}; cache hits {report['cache_hits']},"
        f" misses {report['cache_misses']}; max cosine distance between repeats"
        + "".join(
            f" {name} {v['max_cosine_distance']:.3g} ({v['pairs']} pairs)"
            for name, v in variation.items()
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main_cli(sys.argv[1:], os.environ))
