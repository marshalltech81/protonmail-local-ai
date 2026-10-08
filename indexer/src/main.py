"""
Indexer entry point.
Watches the Maildir for new/changed emails, parses and threads them,
generates embeddings via an OpenAI-compatible /v1/embeddings endpoint,
and writes to the SQLite index.

The embedder is operator-supplied: any OpenAI-compatible provider
works (OpenAI proper, remote alternatives like DeepInfra / OpenRouter,
or a host-side server the operator installs themselves: LM Studio,
vLLM, ``mlx_lm.server``, TEI). Configure via ``EMBED_MODEL`` +
``EMBED_API_KEY`` Docker secret + ``EMBED_BASE_URL`` (all required,
non-empty). ``EMBED_BASE_URL`` is the endpoint URL, or ``default`` to
use the openai SDK's documented default (OpenAI proper); an empty
value fails startup, because an API key is not consent to the SDK's
default endpoint (#750). Operators pointing at an unauthenticated
host-side server set ``EMBED_BASE_URL`` to the host endpoint and
supply any placeholder string for the key. ``EMBED_MODE`` is the wire-shape
selector kept for symmetry with the other layers; only ``openai`` is
valid today.

By default (mirror mode) the indexer also runs a reconciler that records
tombstones for mbsync-flagged (``T``) Maildir files and reaps them after a
grace window; ``INDEXER_DELETION_ENABLED=false`` (archive mode) turns it
off. See ``src/reconciler.py``.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.parse
import warnings
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from .attachment_indexing import (
    AttachmentWritePlan,
    apply_attachment_writes,
    attachment_outcomes,
    attachment_outcomes_degraded,
    format_attachment_outcomes,
    prepare_attachment_writes,
    record_committed_outcomes,
    reprocess_reruns_extraction,
    too_large_fits,
)
from .chunker import (
    MessageChunk,
    chunk_segments,
    estimate_tokens,
    mean_vector,
    truncate_to_tokens,
)
from .database import EMBEDDING_DIM, SCHEMA_VERSION, Database
from .embed_identity import (
    CalibrationRequestError,
    EmbedderDimensionError,
    EmbedderIdentityError,
    verify_or_record_embedder,
)
from .embedder import (
    EMBED_FAILURE_CONFIGURATION,
    EMBED_FAILURE_REJECTED_INPUT,
    EMBED_FAILURE_UNCERTAIN,
    EmbeddingBackend,
    EmbedResponseError,
    OpenAIEmbedder,
    classify_embed_failure,
    scrub_embed_error,
)
from .entities import AuthorityRules, AuthorityRulesError, load_authority_rules
from .extractors import (
    DEFAULT_MAX_BYTES,
    ExtractionResult,
    drain_suppressed_lines,
    is_stale_extractor,
    warn_rate_limited,
)
from .folder_watch import FolderWatchRefresher
from .maildir import (
    PERMS_REPAIRED_NAME,
    SYNC_STAMP_NAME,
    SyncStamp,
    is_trashed,
    parse_sync_stamp_rename,
    read_sync_stamp,
)
from .parser import (
    Message,
    OversizedMessageError,
    _derive_folder,
    message_sort_time,
    parse_email,
)
from .queue import (
    ERROR_CLASS_OPERATOR,
    ERROR_CLASS_RETRYABLE,
    INTERRUPTED_STAGE,
    PERMISSION_DEFERRED_ERROR,
    REASON_INITIAL_SCAN,
    REASON_ON_CREATED,
    REASON_ON_MOVED,
    REASON_RECOVERY,
    REASON_REEXTRACT,
    REASON_REPARSE,
    REASON_RESCAN,
    STAGE_EMBED,
    STAGE_PARSE,
    STAGE_TRASHED,
    IndexingQueue,
)
from .queue import load_config_from_env as load_queue_config_from_env
from .quoting import segment_for_embedding
from .reconciler import Reconciler, ReconcilerConfig, load_config_from_env, sweep_paths
from .stall_guard import StallGuard
from .threader import Thread, Threader, subject_embed_line
from .timings import StageTimings, TimingAggregator, format_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("indexer")


# --- Startup identity (#887) -------------------------------------------------
# One line naming what is running, logged when this module is imported:
# before any setting below is parsed, so a malformed setting, a refused
# index or a failed migration still leaves it in the log (Codex review
# rounds 3 and 4 on #893). Every input is a raw environment string or a
# read that never raises.

# A source commit as the Makefile passes it (``git rev-parse --short
# HEAD``, plus ``-dirty``): anything else is logged as ``unknown``, so a
# stray value cannot add text or a line to the log.
_GIT_COMMIT_PATTERN = re.compile(r"[0-9A-Za-z._-]{1,64}")

# The settings the config hash covers, named one by one. Non-secret
# values only (paths, modes, the endpoint, the model and limits): never
# ``EMBED_API_KEY`` or any other secret, and never the whole environment.
_IDENTITY_SETTINGS = (
    "MAILDIR_PATH",
    "SQLITE_PATH",
    "INDEXER_HEALTH_FILE",
    "EMBED_MODE",
    "EMBED_BASE_URL",
    "EMBED_MODEL",
    "EMBED_BATCH_SIZE",
    "EMBED_CONCURRENCY",
    "EMBED_WARMUP_TIMEOUT_SECS",
    "INDEXER_PARSE_MAX_BYTES",
    "INDEXER_CHUNK_TARGET_TOKENS",
    "INDEXER_CHUNK_MAX_TOKENS",
    "INDEXER_CHUNK_OVERLAP_TOKENS",
    "INITIAL_INDEX_BATCH_SIZE",
    "INDEXER_STEADY_STATE_BATCH_SIZE",
    "INDEXER_WAL_CHECKPOINT_INTERVAL_SECS",
    "INDEXER_RECOVERY_SWEEP_INTERVAL_SECS",
    "INDEXER_MESSAGE_TIMEOUT_SECONDS",
    "INDEXER_ATTACHMENT_EXTRACTION_ENABLED",
    "INDEXER_OCR_ENABLED",
    "INDEXER_ATTACHMENT_MAX_BYTES",
    "INDEXER_OCR_MAX_PAGES",
    "INDEXER_OCR_TIMEOUT_SECONDS",
    "INDEXER_PDF_MAX_DIGITAL_PAGES",
    "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS",
    "INDEXER_MAX_ATTEMPTS",
    "INDEXER_RETRY_BASE_SECONDS",
    "INDEXER_DELETION_ENABLED",
    "INDEXER_DELETION_GRACE_DAYS",
    "INDEXER_DELETION_SWEEP_INTERVAL_SECS",
    "INDEXER_DELETION_MAX_BATCH_PCT",
    "INDEXER_DELETION_FORCE",
)

# Every other variable ``src/`` reads, with why it is not hashed.
# ``tests/test_startup_identity.py`` checks that each variable read is in
# one of the two lists (Codex review round 5 on #893).
_IDENTITY_EXCLUDED = {
    "EMBED_API_KEY": "a secret: the embedder credential",  # pragma: allowlist secret
    "GIT_COMMIT": "not configuration: logged as the line's own commit field",
}


def _git_commit() -> str:
    """The commit the image was built from (``GIT_COMMIT``, baked in by
    the Dockerfile), or ``unknown``."""
    value = os.environ.get("GIT_COMMIT", "").strip()
    return value if _GIT_COMMIT_PATTERN.fullmatch(value) else "unknown"


def _hashable_url(value: str) -> str:
    """``value`` unless it carries userinfo, so the hash cannot be used to
    test guesses at a password offline (Codex round 6 on #893). Never
    raises: a value ``urlsplit`` rejects is a fixed marker too."""
    try:
        parts = urllib.parse.urlsplit(value)
        if parts.username is not None or parts.password is not None:
            return "<url-with-credentials>"
    except ValueError:
        return "<unparseable-url>"
    return value


def _identity_settings() -> dict[str, str | None]:
    """The raw configured value of each ``_IDENTITY_SETTINGS`` name,
    ``None`` when unset. Not parsed, so it cannot raise: a malformed
    value just hashes differently, and an unset setting differs from one
    set to its default. The one exception is a ``*_BASE_URL`` carrying
    credentials, hashed as a marker (``_hashable_url``)."""
    settings = {name: os.environ.get(name) for name in _IDENTITY_SETTINGS}
    for name, value in settings.items():
        if name.endswith("_BASE_URL") and value is not None:
            settings[name] = _hashable_url(value)
    return settings


def _config_hash(settings: dict[str, str | None]) -> str:
    """First 12 hex digits of a SHA-256 over ``settings`` as sorted JSON."""
    canonical = json.dumps(settings, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _read_stored_schema_version(path: Path) -> str:
    """The schema version stamped in the index file, read read-only.

    Never creates or migrates the file and never raises. ``none`` when
    the file, table or row is missing; ``unreadable`` when it cannot be
    read (``Database`` reports why when it opens the file).
    """
    try:
        # ``stat`` rather than ``exists()``, which reports a file it may
        # not stat as missing (Codex round 6 on #893).
        try:
            path.stat()
        except FileNotFoundError:
            return "none"
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if table is None:
                return "none"
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        return "none" if row is None else str(int(row[0]))
    except OSError, RuntimeError, sqlite3.Error, TypeError, ValueError:
        return "unreadable"


def _log_startup_identity() -> None:
    """Log one line naming what is running (#887): the source commit, a
    random ID for this start, the code's schema version, the one stamped
    in the index file before any migration, and the first 12 hex digits
    of a SHA-256 over the raw ``_IDENTITY_SETTINGS`` values."""
    stored = _read_stored_schema_version(Path(os.environ.get("SQLITE_PATH", "/data/mail.db")))
    log.info(
        "Startup identity: service=indexer commit=%s boot=%s schema_code=%d "
        "schema_stored=%s config=%s",
        _git_commit(),
        secrets.token_hex(6),
        SCHEMA_VERSION,
        stored,
        _config_hash(_identity_settings()),
    )


_log_startup_identity()


def quiet_document_libraries() -> None:
    """Keep third-party document parsers' per-document output out of the log.

    pypdf and Pillow log, and openpyxl and Pillow warn, with values read
    from the attachment: font dictionaries, encoding names, cell values,
    TIFF tags (#690). The extractors' own fixed-text logging and the
    ``attachment_extractions`` status still report every outcome. The
    image extractor's ``DecompressionBombWarning``-to-error filter is set
    inside its own ``catch_warnings`` scope, so it still takes precedence.
    """
    for name in ("pypdf", "PIL"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    for package in ("openpyxl", "PIL"):
        warnings.filterwarnings("ignore", module=rf"{package}(\.|$)")


quiet_document_libraries()

MAILDIR_PATH = Path(os.environ.get("MAILDIR_PATH", "/maildir"))
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "/data/mail.db"))
# Operator source-authority rules (``config/authority.toml.example``),
# at the fixed path where compose mounts ``./config`` read-only.
# Absent: every entity is unclassified. Malformed: startup fails.
AUTHORITY_RULES_PATH = Path("/config/authority.toml")

# OpenAI-compatible embedder configuration. The operator supplies the
# provider. Set ``EMBED_MODEL`` to a model id served at the chosen
# endpoint; the schema reserves a fixed 4096-dim vector so pick a
# 4096-dim model (Qwen3-Embedding-8B variants) or run a schema
# migration. Set ``EMBED_BASE_URL`` to any compliant /v1 base URL, or
# to ``default`` to use the openai SDK's documented default (OpenAI
# proper at ``https://api.openai.com/v1``); empty fails startup (#750).
# The bearer credential is loaded from the ``embed_api_key`` Docker
# secret or ``EMBED_API_KEY`` env and is required (non-empty);
# unauthenticated host-side servers accept any placeholder string.
# ``EMBED_MODE`` is kept as a config knob for symmetry with
# ``INFERENCE_MODE`` / ``RERANK_MODE`` but only accepts ``openai``
# today — embed is the headline retrieval feature and the indexer
# cannot function without it, so there is no disabled mode.
_EMBED_MODES = frozenset({"openai"})


def _normalize_embed_mode(raw: str) -> str:
    mode = raw.strip().lower()
    if mode in _EMBED_MODES:
        return mode
    raise ValueError("EMBED_MODE must be 'openai'")


EMBED_MODE = _normalize_embed_mode(os.environ.get("EMBED_MODE", "openai"))
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "").strip()


# Endpoint hosts that keep a provider call on this machine: the host's
# loopback, or OrbStack's route from a container to it.
_HOST_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "host.docker.internal"})


def _warn_if_remote_endpoint(mode_setting: str, mode: str, url: str, sends: str) -> None:
    """Log one WARNING when the embedder endpoint is not host-local.

    ``url`` is the resolved endpoint; empty means an SDK default, which
    is always a remote provider. Only the host is named: never the
    path, query, port or the API key. Mirrors the mcp-server helper.
    """
    host = urllib.parse.urlsplit(url).hostname if url else None
    if host in _HOST_LOCAL_HOSTS:
        return
    log.warning(
        "Privacy: %s=%s sends %s off this host, to %s.",
        mode_setting,
        mode,
        sends,
        host or "the SDK's default endpoint",
    )


# The literal ``EMBED_BASE_URL`` value that selects OpenAI's official
# endpoint, compared trimmed and case-insensitively, and the URL it
# resolves to. The URL is passed to the SDK explicitly: left out, the
# SDK would read ``OPENAI_BASE_URL`` first, which could send mail
# somewhere the operator did not choose (Codex round 1 on #773).
_SDK_DEFAULT_BASE_URL = "default"
_OPENAI_DEFAULT_URL = "https://api.openai.com/v1"


def _resolve_base_url(name: str, raw: str, default_url: str) -> str:
    """Return an enabled layer's base URL; ``default`` returns
    ``default_url``, the provider's official endpoint.

    An API key is not consent to the SDK's default endpoint: the request
    body (mail text) is sent before the provider checks the key, so a
    placeholder key plus a forgotten URL would ship mail to a cloud
    provider (owner decision 2026-10-05, #750). An empty value therefore
    fails startup. ``default`` resolves to the official URL, passed to
    the SDK explicitly so ``OPENAI_BASE_URL`` cannot redirect it. The
    error is fixed text and never echoes a value.
    """
    value = raw.strip()
    if not value:
        host = urllib.parse.urlsplit(default_url).hostname
        raise ValueError(
            f"{name} is empty: set it to the provider's URL, or to `default` to use "
            f"the SDK's default endpoint (sends mail to {host})."
        )
    if value.lower() == _SDK_DEFAULT_BASE_URL:
        return default_url
    return value


def _validate_embed_config() -> str:
    """Raise at startup when the embedder is misconfigured; return the
    base URL to build the embedder with (``default`` resolved to
    OpenAI's official URL).

    Validation runs in ``main()`` rather than at module load so test
    files can ``from src import main`` to import helper functions
    without supplying a full embedder config. The container entrypoint
    always reaches ``main()`` first, so the operator-facing failure
    surface is identical.

    ``EMBED_API_KEY`` is required (non-empty); ``EMBED_MODEL`` is too.
    ``EMBED_BASE_URL`` must be a URL or ``default`` (OpenAI proper via
    the openai SDK); empty fails (``_resolve_base_url``, #750), the same
    rule the mcp-server applies to every enabled layer. Operators
    pointing at an unauthenticated host-side server (LM Studio, vLLM,
    ``mlx_lm.server``, TEI) supply any placeholder string for
    ``EMBED_API_KEY``; the compat server ignores the bearer header.
    """
    if not EMBED_MODEL.strip():
        raise ValueError("EMBED_MODEL must be set when EMBED_MODE='openai'")
    if not EMBED_API_KEY:
        raise ValueError("EMBED_API_KEY must be set when EMBED_MODE='openai'")
    base_url = _resolve_base_url("EMBED_BASE_URL", EMBED_BASE_URL, _OPENAI_DEFAULT_URL)
    # Reject URLs that embed a ``user:pass@host`` userinfo authority.
    # The resolved base URL flows into the startup log line naming the
    # wire endpoint, so embedded credentials would leak to container
    # logs / journald. The credential model puts every secret in a
    # Docker-secrets file (``.secrets/embed_api_key.txt``). Mirrors the
    # same guard in ``scripts/validate-env.sh`` so a deployment that
    # skipped that script still fails closed instead of leaking.
    if base_url and "@" in urllib.parse.urlsplit(base_url).netloc:
        raise ValueError(
            "EMBED_BASE_URL must not embed credentials (user:pass@host). Put "
            "the API key in .secrets/embed_api_key.txt instead."
        )
    return base_url


def _read_embed_api_key() -> str:
    """Read the embedder API key from a Docker secret, then env, then empty.

    Mirrors the secret-then-env pattern used in mcp-server. The Docker
    secret path follows the existing ``/run/secrets/<name>`` convention;
    ``EMBED_API_KEY`` env is the fallback for non-Docker deployments.

    An empty return value is not an error here — the startup contract
    is enforced by ``_validate_embed_config()``, which fails closed if
    no key is set. Splitting "read" from "require" keeps the reader
    purely about source-of-truth precedence (secret > env), and the
    validator owns the non-empty rule. For unauthenticated host-side
    servers the operator supplies a placeholder string (e.g.
    ``unauthenticated``); the SDK sends it as a bearer token that
    compat servers ignore.

    Fail-closed posture: when the Docker secret file *exists* but cannot
    be read (perms regression, mount issue), this is a deployment
    misconfiguration — propagate the error so the indexer fails to
    start rather than silently sending an empty bearer token (or worse,
    a stale env value the operator thought the secret had superseded).
    The env fallback only kicks in when the secret file is absent.
    """
    secret_path = Path("/run/secrets/embed_api_key")
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()
    return os.environ.get("EMBED_API_KEY", "").strip()


EMBED_API_KEY = _read_embed_api_key()
# /tmp default is safe: container tmpfs, non-root user, overridable via env.
INDEXER_HEALTH_FILE = Path(os.environ.get("INDEXER_HEALTH_FILE", "/tmp/indexer-health"))  # nosec B108


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Read an int >= ``minimum`` from the environment.

    Unset or empty yields ``default``. A non-integer or a value below
    ``minimum`` raises ``ValueError`` naming the variable, so a typo
    stops startup instead of silently changing behaviour (#481). Same
    rule as ``queue.load_config_from_env`` and
    ``reconciler.load_config_from_env``.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    if value < minimum:
        raise ValueError(f"{name}={raw!r} must be >= {minimum}")
    return value


# How many texts the embedder client packs into a single
# ``/v1/embeddings`` HTTP call. Larger batches amortize per-request
# overhead — meaningful for remote providers, marginal for a host-side
# server on loopback. The provider's own per-request input cap is the
# upper bound (DeepInfra accepts 1024, per the ``input`` maxLength in
# its OpenAI-compatible embeddings API reference; OpenAI accepts 2048).
EMBED_BATCH_SIZE = _int_env("EMBED_BATCH_SIZE", 64)

# How many of those requests one embed call keeps in flight (#713). A
# remote provider spends most of the indexing time computing vectors, so
# overlapping requests shortens a large index; 1 sends them one after
# another, which suits a host-side server that serves one at a time.
EMBED_CONCURRENCY = _int_env("EMBED_CONCURRENCY", 1)


# Chunker token budgets — see ``chunker.chunk_message`` for semantics.
# Defaults are sized for the MLX-served Qwen3-Embedding-8B context
# window. Qwen3-Embedding handles long context cleanly, so the
# default ``max`` is 1500 — fewer, larger chunks reduce embed call
# count and produce better mean-of-chunks thread vectors.
CHUNK_TARGET_TOKENS = _int_env("INDEXER_CHUNK_TARGET_TOKENS", 1000)
CHUNK_MAX_TOKENS = _int_env("INDEXER_CHUNK_MAX_TOKENS", 1500)
CHUNK_OVERLAP_TOKENS = _int_env("INDEXER_CHUNK_OVERLAP_TOKENS", 150, minimum=0)


def _check_chunk_budgets(target: int, max_tokens: int, overlap: int) -> None:
    """Raise ``ValueError`` unless the chunk budgets fit together.

    ``chunker.chunk_message`` requires ``target <= max`` and
    ``overlap < target`` (each budget's own minimum is enforced by
    ``_int_env``). It checks this per message, so a bad combination
    would start cleanly and then dead-letter every message; check it
    once here instead (#507).
    """
    if target > max_tokens:
        raise ValueError(
            f"INDEXER_CHUNK_TARGET_TOKENS={target} must be <= INDEXER_CHUNK_MAX_TOKENS={max_tokens}"
        )
    if overlap >= target:
        raise ValueError(
            f"INDEXER_CHUNK_OVERLAP_TOKENS={overlap} must be < INDEXER_CHUNK_TARGET_TOKENS={target}"
        )


_check_chunk_budgets(CHUNK_TARGET_TOKENS, CHUNK_MAX_TOKENS, CHUNK_OVERLAP_TOKENS)

# How many messages the initial-scan drainer accumulates before issuing
# a single batched embed call. Larger batches amortize the embed
# round-trip across more messages — meaningful when EMBED_BASE_URL
# points at a remote provider (~150 ms RTT each), marginal against a
# host-side server on loopback.
INITIAL_INDEX_BATCH_SIZE = _int_env("INITIAL_INDEX_BATCH_SIZE", 50)


# Steady-state (post-initial-scan) batch size for the main-loop drain.
# Smaller than the initial-scan size because steady-state typically sees
# 1-3 messages per pass, but routing through the same batched path means
# (a) a burst from an mbsync sync still gets one bulk embed call instead
# of one HTTP round-trip per message (decisive against any cloud
# embedder), and (b) the initial-scan and steady-state code paths share
# one Phase 1/2 implementation rather than diverging on seed-vector
# selection.
STEADY_STATE_BATCH_SIZE = _int_env("INDEXER_STEADY_STATE_BATCH_SIZE", 8)

# How often (seconds) the main loop runs ``_run_wal_maintenance``: the
# FTS5 scrub of tables a reap deleted from, then
# ``Database.wal_checkpoint_truncate``.
# An open connection does not pin the WAL; only an open read transaction
# does. SQLite's automatic checkpoint lets the WAL be reused from the
# start once its frames are checkpointed, but it never shrinks the file,
# so the WAL stays at its high-water size after a burst of writes. The
# explicit truncate-checkpoint reclaims that space. 10 min keeps the file
# size bounded without churning IO.
WAL_CHECKPOINT_INTERVAL_SECS = _int_env("INDEXER_WAL_CHECKPOINT_INTERVAL_SECS", 600, minimum=60)

# How often (seconds) the main loop runs ``_recover_zero_vector_threads``.
# The periodic sweep recovers retryable shapes — chunkless zero-vector
# threads whose queue row is still 'queued' or has been cleaned up —
# but does NOT auto-resurrect dead-lettered rows. Dead = operator-
# visible terminal state; clearing it requires explicit intervention
# (``make requeue-dead``).
# See ``_recover_zero_vector_threads`` for the full policy. The same
# cadence drives the periodic Maildir rescan
# (``_enqueue_unindexed_messages``).
RECOVERY_SWEEP_INTERVAL_SECS = _int_env("INDEXER_RECOVERY_SWEEP_INTERVAL_SECS", 1800, minimum=60)

# Phase 1 seed for new threads and already-zero chunkless ones (the
# only branch that uses this constant). Phase 1's seed-selection runs a three-case priority
# chain:
#   1. Thread has chunk vectors → mean(chunks).
#   2. No chunks but a prior threads_vec row carries a non-zero
#      embedding → preserve that. Covers chunkless subject-fallback
#      threads — protects them from Phase 2 failure clobbering.
#   3. Neither → this zero placeholder. Truly new threads, or
#      post-crash recovery on a thread that already had a zero row.
# A zero vector is safe between phases: cosine/L2 similarity to a
# normalized query vector is constant, so a zero-vec thread cannot
# inflate ranking on any specific query — it just deprioritizes
# uniformly until Phase 2c lands the real vector. Keyword search via
# threads_fts is unaffected.
_ZERO_THREAD_VECTOR = [0.0] * EMBEDDING_DIM


def _bool_env(name: str, default: bool) -> bool:
    """Read a boolean from the environment.

    Unset or empty yields ``default``; any value outside the accepted
    vocabulary raises ``ValueError`` so a typo such as
    ``INDEXER_OCR_ENABLED=tru`` stops startup instead of disabling the
    feature (#481). Same vocabulary as ``reconciler.load_config_from_env``.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}={raw!r} is not recognized; use true or false")


# Attachment extraction — see ``src/extractors/`` for
# per-format implementations and ``.env.example`` for the operator-
# facing reference. Defaults are conservative: OCR enabled (most
# valuable for scanned receipts and screenshots), 32 MiB attachment
# cap (skips huge backup zips but reads the 10-30 MB scanned reports
# common in real mail, #693), 20-page OCR cap (bounds CPU on scanned
# books). The cap bounds only extraction: the parser has already
# decoded the payload, and an ``.eml`` under the default
# ``INDEXER_PARSE_MAX_BYTES`` (50 MB) carries at most ~36 MB of
# base64-encoded attachment, so a much larger default would admit
# little more.
_DEFAULT_ATTACHMENT_MAX_BYTES = DEFAULT_MAX_BYTES
INDEXER_ATTACHMENT_EXTRACTION_ENABLED = _bool_env("INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
INDEXER_OCR_ENABLED = _bool_env("INDEXER_OCR_ENABLED", True)
INDEXER_ATTACHMENT_MAX_BYTES = _int_env(
    "INDEXER_ATTACHMENT_MAX_BYTES", _DEFAULT_ATTACHMENT_MAX_BYTES, minimum=1
)
INDEXER_OCR_MAX_PAGES = _int_env("INDEXER_OCR_MAX_PAGES", 20, minimum=1)
# Per-page OCR time ceiling. Tesseract is single-threaded and a
# crafted high-noise image (still inside ``INDEXER_ATTACHMENT_MAX_BYTES``)
# can keep it busy for minutes; combined with ``INDEXER_OCR_MAX_PAGES``
# that pins the worker for tens of minutes per PDF. Set to 0 to
# disable the timeout.
INDEXER_OCR_TIMEOUT_SECONDS = _int_env("INDEXER_OCR_TIMEOUT_SECONDS", 60, minimum=0)
# Longest one unit of work — a message's parse, or one attachment's
# extraction — may run before the stall guard exits the process for
# Compose to restart (``stall_guard``). Each attachment restarts the
# clock, so a message with many slow attachments is fine; one scanned
# PDF legitimately takes up to ``INDEXER_OCR_MAX_PAGES`` x
# ``INDEXER_OCR_TIMEOUT_SECONDS`` plus its render (~21 min at the
# defaults). Set to 0 to disable.
INDEXER_MESSAGE_TIMEOUT_SECONDS = _int_env("INDEXER_MESSAGE_TIMEOUT_SECONDS", 3600, minimum=0)
# Page cap for the digital pypdf path. The OCR cap above doesn't bound
# this — a 5 MB text-only PDF can carry thousands of pages, and even
# at ~ms per page the indexer queue stalls. Set to 0 to disable.
INDEXER_PDF_MAX_DIGITAL_PAGES = _int_env("INDEXER_PDF_MAX_DIGITAL_PAGES", 500, minimum=0)
# 2,000,000 chars ~ 500 pages of dense OCR text. Bounds the
# ``attachment_extractions.extracted_text`` row size so a single huge
# scanned PDF can't blow up SQLite by storing tens of MB of text per
# attachment. Set to 0 to disable the cap.
INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS = _int_env(
    "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", 2_000_000, minimum=0
)


# Recurring steps whose failures are logged where they happen and whose
# recovery ``_FailureStreak`` logs (#873). A fixed set, so a recovery
# line carries a known name and counts, never text from the failure.
HEALTH_FILE_REFRESH = "health file refresh"
INGESTION_STATE_RECORDING = "ingestion state recording"
WATCH_REFRESH = "Maildir watch refresh"
PERIODIC_RECONCILIATION = "periodic reconciliation"
PERIODIC_RESCAN = "periodic Maildir rescan"
WAL_CHECKPOINT = "wal checkpoint"
REAPED_RECORD_PRUNE = "reaped-record prune"
RECOVERY_COMPONENTS = frozenset(
    {
        HEALTH_FILE_REFRESH,
        INGESTION_STATE_RECORDING,
        WATCH_REFRESH,
        PERIODIC_RECONCILIATION,
        PERIODIC_RESCAN,
        WAL_CHECKPOINT,
        REAPED_RECORD_PRUNE,
    }
)


class _FailureStreak:
    """One recurring step's run of consecutive failures (#873).

    The step logs each failure itself; this keeps the count and when the
    run began, and the first success after one or more failures logs one
    INFO line, so an outage of that step has a visible end. Every step
    runs on the main thread.
    """

    def __init__(self, component: str):
        if component not in RECOVERY_COMPONENTS:
            raise ValueError("unknown recovery component")
        self.component = component
        self.failures = 0
        self.since = 0.0

    def failed(self) -> None:
        if not self.failures:
            self.since = _monotonic()
        self.failures += 1

    def succeeded(self) -> None:
        if not self.failures:
            return
        log.info(
            "%s recovered after %d failure(s) over %ds",
            self.component,
            self.failures,
            _monotonic() - self.since,
        )
        self.failures = 0


_streaks = {name: _FailureStreak(name) for name in RECOVERY_COMPONENTS}


# The Maildir watcher, set by ``main`` once it is started;
# ``touch_health_file`` checks it on every heartbeat. watchdog's
# dispatcher catches only ``queue.Empty``, so an exception escaping a
# handler (``enqueue`` or ``is_indexed`` on a locked database, a full
# disk) ends the observer thread, and from then on only the periodic
# rescan finds new mail. The initial drain can run for hours before the
# main loop starts, refreshing the health file after every message, so
# the check rides on the heartbeat rather than the loop: the
# healthcheck never reports a watcherless indexer as live, and the exit
# is the stall guard's remedy, Compose restarts the container with a
# fresh watcher and the startup walk covers the gap (#870).
_observer: BaseObserver | None = None


def _exit_if_watcher_dead() -> None:
    if _observer is None or _observer.is_alive():
        return
    log.error(
        "Maildir watcher thread stopped; new mail is found only by the "
        "periodic rescan; exiting so the container restarts"
    )
    raise SystemExit(1)


def touch_health_file() -> None:
    _exit_if_watcher_dead()
    try:
        INDEXER_HEALTH_FILE.touch(exist_ok=True)
    except OSError:
        _streaks[HEALTH_FILE_REFRESH].failed()
        raise
    _streaks[HEALTH_FILE_REFRESH].succeeded()
    # Liveness for get_mailbox_status rides on every health heartbeat
    # (per message, per embed batch), so a long drain pass never reads
    # as a stopped indexer.
    if _ingestion_state is not None:
        _ingestion_state.maybe_record(time.monotonic())


def _extraction_heartbeat() -> None:
    """``touch_health_file`` for an extractor's per-page progress.

    The dispatcher records any exception an extractor raises as a
    ``failed`` extraction of the payload, so a heartbeat write error must
    not escape into it. A file that cannot be refreshed goes stale, and
    the healthcheck reports that.
    """
    try:
        touch_health_file()
    except OSError as e:
        # Once per attachment page while the file cannot be written.
        warn_rate_limited(log, "health file refresh failed: %s", type(e).__name__, attachment=False)


class _IngestionStateRecorder:
    """Writes the last acknowledged mbsync sync and the indexer's
    liveness into ``ingestion_state`` for mcp-server's
    ``get_mailbox_status``.

    A sync is acknowledged only once every message it delivered is
    queued: when the watcher handles the stamp's rename (watchdog
    dispatches events in order, so the sync's delivery events were
    handled first), or when a Maildir walk that started after the sync
    finishes. A stamp merely present on disk proves nothing — the
    watcher may still be behind on that sync's deliveries.

    Writes at most once per ``interval_secs``; mcp-server's staleness
    thresholds are minutes.
    """

    def __init__(self, db: Database, maildir_root: Path, interval_secs: float = 30):
        self.db = db
        self.maildir_root = maildir_root
        self.interval_secs = interval_secs
        self._acked: SyncStamp | None = None
        self._last_write: float | None = None

    def read_stamp(self) -> SyncStamp | None:
        """The stamp on disk now, for acknowledging after a walk."""
        try:
            return read_sync_stamp(self.maildir_root)
        except (OSError, ValueError) as e:
            log.warning("unreadable mbsync sync stamp %s: %s", SYNC_STAMP_NAME, e)
            return None

    def acknowledge(self, stamp: SyncStamp | None) -> None:
        # Called from the watcher thread and the main thread; keep the
        # newest so a walk that read an older stamp cannot move it back.
        # Timestamps share one UTC ISO format, so they compare as strings.
        # An acknowledged stamp ahead of our clock (the clock rolled back)
        # yields to any new one; otherwise every later sync would sort
        # earlier and be dropped until the clock caught up (#332).
        acked = self._acked
        if stamp is None:
            return
        if (
            acked is None
            or stamp.completed_at > acked.completed_at
            or datetime.fromisoformat(acked.completed_at) > datetime.now(UTC)
        ):
            self._acked = stamp

    def maybe_record(self, now: float) -> None:
        if self._last_write is not None and now - self._last_write < self.interval_secs:
            return
        acked = self._acked
        try:
            self.db.record_ingestion_state(
                sync_completed_at=acked.completed_at if acked else None,
                sync_interval_secs=acked.sync_interval_secs if acked else None,
                seen_at=datetime.now(UTC).isoformat(),
            )
        except sqlite3.Error as e:
            # Retried on every heartbeat until a write succeeds.
            _streaks[INGESTION_STATE_RECORDING].failed()
            warn_rate_limited(
                log,
                "recording ingestion state failed: %s",
                type(e).__name__,
                level=logging.ERROR,
                attachment=False,
            )
            return
        _streaks[INGESTION_STATE_RECORDING].succeeded()
        self._last_write = now


# Set by ``main``; ``touch_health_file`` reports liveness through it.
_ingestion_state: _IngestionStateRecorder | None = None


class MaildirHandler(FileSystemEventHandler):
    """Watches Maildir for new email files and enqueues them for indexing.

    The callback path only enqueues — the actual parse / embed / upsert
    pipeline runs in the main loop via ``_drain_queue_batched``. Enqueue is a
    single SQLite write, so the watchdog's internal thread no longer
    blocks on a slow embed round-trip and a Watchdog event storm
    cannot overflow whatever buffer ``watchdog`` uses internally while
    an embed is in flight.
    """

    def __init__(
        self,
        db: Database,
        queue: IndexingQueue,
        reconciler: Reconciler | None = None,
        ingestion_state: _IngestionStateRecorder | None = None,
        sync_completed: threading.Event | None = None,
        directory_created: threading.Event | None = None,
    ):
        self.db = db
        self.queue = queue
        self.reconciler = reconciler
        self.ingestion_state = ingestion_state
        # Set on each mbsync sync stamp and permission-repair marker; the
        # main loop then re-watches folders the repair made readable
        # (#516). The marker follows a failed sync attempt too (#524).
        self.sync_completed = sync_completed
        # Set on every directory the watch reports created: watchdog
        # cannot watch one mbsync created 0700, so the next refresh
        # re-schedules the watch.
        self.directory_created = directory_created

    def _is_reaped_or_deleted(self, path: str | Path) -> bool:
        # With deletion reconciliation enabled, a T-flagged file is
        # deleted upstream. After a reap its .eml always stays on disk
        # unindexed; enqueueing it would resurrect the message into
        # search. Same rule as the Maildir walk's ``skip_trashed``.
        return self.reconciler is not None and is_trashed(path)

    def on_created(self, event):
        if event.is_directory:
            if self.directory_created is not None:
                self.directory_created.set()
            return
        path = Path(event.src_path)
        # Only enqueue files in cur/ or new/ subdirectories
        if path.parent.name in ("cur", "new") and not self._is_reaped_or_deleted(path):
            self.queue.enqueue(str(path), REASON_ON_CREATED)

    def on_moved(self, event):
        # Two distinct move scenarios land here:
        # 1. Flag changes: mbsync renames files in-place within the same
        #    directory when flags change (e.g. ``msg:2,S`` → ``msg:2,SR``
        #    when the message is replied to). The source path is already
        #    in ``indexed_files``. Re-parsing and re-embedding would waste
        #    an embed round-trip and leave stale rows behind — the
        #    reconciler (or fall-through ``update_filepath``) just has to
        #    move the stored filepath to the new name.
        # 2. Maildir delivery: a message is first written under ``tmp/``
        #    and renamed into ``new/`` or ``cur/``. The source path is not
        #    in ``indexed_files`` (it was a temp file), so this is a new
        #    message that must be indexed (``on_created`` does not fire
        #    for rename destinations).
        if event.is_directory:
            return

        src_path = str(event.src_path)
        dest_path_obj = Path(event.dest_path)
        dest_path = str(dest_path_obj)

        # mbsync renamed a new last-sync stamp into place. Acknowledge the
        # sync named by the temporary file, not the stamp's current
        # content: a later sync may already have replaced it while its
        # deliveries are still behind this event.
        if dest_path_obj.name == SYNC_STAMP_NAME and dest_path_obj.parent == MAILDIR_PATH:
            stamp = parse_sync_stamp_rename(src_path)
            if stamp is None:
                log.warning("ignoring unrecognized rename onto the mbsync sync stamp")
            else:
                if self.ingestion_state is not None:
                    self.ingestion_state.acknowledge(stamp)
                if self.sync_completed is not None:
                    self.sync_completed.set()
            return

        # mbsync finished a permission repair, possibly for a failed sync
        # attempt: re-watch, but acknowledge no sync (#524).
        if dest_path_obj.name == PERMS_REPAIRED_NAME and dest_path_obj.parent == MAILDIR_PATH:
            if self.sync_completed is not None:
                self.sync_completed.set()
            return

        if self.db.is_indexed(src_path):
            # Case 1: rename of an existing indexed message. Flag renames
            # stay in one folder; a move across folders also carries the
            # new folder, recorded atomically with the locator. If that
            # write fails it rolls back whole: the destination stays
            # unindexed and the periodic Maildir walk re-indexes it.
            dest_folder = _derive_folder(dest_path_obj, MAILDIR_PATH)
            folder_change = (
                dest_folder if dest_folder != _derive_folder(Path(src_path), MAILDIR_PATH) else None
            )
            if self.reconciler is not None:
                try:
                    self.reconciler.handle_moved(src_path, dest_path, folder=folder_change)
                except Exception as e:
                    log.error("reconciler on_moved failed: %s", type(e).__name__)
            else:
                # Archive mode has no reconciler; still move the
                # indexed_files / message_thread_map filepath forward so
                # future lookups find the current on-disk name. A rename
                # that drops the T flag restores the message, so clear a
                # tombstone an earlier mirror-mode run left on it (same
                # rule as ``Reconciler.handle_moved``) rather than carry
                # it to the live name, where it would report the message
                # as pending deletion for ever (#860). Archive mode never
                # reaps, so a leftover tombstone otherwise outlives the
                # restore. The lookup stays inside the error boundary:
                # watchdog does not catch handler exceptions, and one
                # escaping here would end the observer thread.
                restored = not is_trashed(dest_path_obj)
                try:
                    leftover_tombstone = restored and self.db.has_pending_deletion(src_path)
                    self.db.update_filepath(
                        src_path, dest_path, folder=folder_change, clear_tombstone=restored
                    )
                except Exception as e:
                    log.error("update_filepath failed on rename: %s", type(e).__name__)
                    return
                if leftover_tombstone:
                    # One line per restore, within the indexer's shared
                    # per-window budget: a folder restored at once fires
                    # one event per message, and the rest are counted on
                    # the queue heartbeat's ``suppressed_lines``.
                    warn_rate_limited(
                        log,
                        "archive retention: cleared %d leftover tombstone on restore",
                        1,
                        level=logging.INFO,
                        attachment=False,
                    )
            return

        # Case 2: new delivery — enqueue for the worker. A flag rename
        # of an already-reaped file also lands here (its source is no
        # longer indexed) and must not be mistaken for new mail.
        if (
            dest_path_obj.parent.name in ("cur", "new")
            and not self.db.is_indexed(dest_path)
            and not self._is_reaped_or_deleted(dest_path)
        ):
            self.queue.enqueue(dest_path, REASON_ON_MOVED)


# Emit a p50/p95/max timing summary roughly once per this many processed
# messages. The aggregator itself has an independent rolling window —
# this constant only controls how often the line is logged, not how many
# samples back the percentiles look. It is unrelated to the health-file
# heartbeat, which is refreshed per message, around each embed call and
# after each attachment page read.
TIMING_LOG_EVERY = 25

# In steady state the summaries are also logged when a drain empties the
# queue, and at least this often while counts are pending, so a small
# burst's attachment outcomes are not held back until unrelated mail
# arrives (review round 1 on #884).
SUMMARY_MAX_INTERVAL_SECS = 300.0

# At most one attachments line per this many seconds (review round 4 on
# #884): a drain ending every message would otherwise log one line per
# message. Counts held back carry over to the next line.
OUTCOMES_LOG_MIN_INTERVAL_SECS = 60.0
_last_outcomes_log: float | None = None
_monotonic = time.monotonic


def _log_attachment_outcomes(*, force: bool = False) -> None:
    """Log the attachments committed since the last line by outcome, and
    reset the counts; nothing when there were none. Logged with each
    timing summary, so failed or skipped extractions are visible without
    a line per attachment (#871). WARNING when any count means text is
    missing from search, else INFO. Within
    ``OUTCOMES_LOG_MIN_INTERVAL_SECS`` of the last line nothing is logged
    or reset unless ``force`` (the initial index's final summary)."""
    global _last_outcomes_log
    now = _monotonic()
    if (
        not force
        and _last_outcomes_log is not None
        and now - _last_outcomes_log < OUTCOMES_LOG_MIN_INTERVAL_SECS
    ):
        return
    counts = attachment_outcomes.drain()
    line = format_attachment_outcomes(counts)
    if not line:
        return
    _last_outcomes_log = now
    if attachment_outcomes_degraded(counts):
        log.warning(line)
    else:
        log.info(line)


# The queue heartbeat (#874): counts only, so a stalled or growing queue
# is visible even while nothing drains.
QUEUE_HEARTBEAT_INTERVAL_SECS = 300.0
_last_queue_heartbeat: float | None = None


@dataclass
class _ReparseProgress:
    """Reparse jobs committed (#1078): ``reparsed`` since the indexer
    started, ``logged`` as of the last progress line, and whether that
    line saw a backlog, so the heartbeat logs one completion line."""

    reparsed: int = 0
    logged: int = 0
    active: bool = False


_reparse_progress = _ReparseProgress()


def _log_reparse_progress(remaining: int, dead: int) -> None:
    """With the queue heartbeat: one progress line per interval while
    reparse jobs are queued, then one completion line (WARNING when some
    dead-lettered). Counts and fixed text only."""
    p = _reparse_progress
    if remaining:
        log.info(
            "reparse: remaining=%d reparsed_since_last_heartbeat=%d dead=%d",
            remaining,
            p.reparsed - p.logged,
            dead,
        )
        p.logged = p.reparsed
        p.active = True
        return
    if not p.active:
        return
    p.active = False
    p.logged = p.reparsed
    if dead:
        log.warning(
            "reparse complete: %d message(s) reparsed since the indexer started, "
            "%d dead-lettered (make requeue-dead retries them)",
            p.reparsed,
            dead,
        )
    else:
        log.info(
            "reparse complete: %d message(s) reparsed since the indexer started, 0 dead-lettered",
            p.reparsed,
        )


def _maybe_log_queue_heartbeat(queue: IndexingQueue) -> None:
    """Log the queue's state at most once per
    ``QUEUE_HEARTBEAT_INTERVAL_SECS``. Called each main-loop tick and
    each drain pass, so the hours-long initial drain logs it too."""
    global _last_queue_heartbeat
    now = _monotonic()
    if (
        _last_queue_heartbeat is not None
        and now - _last_queue_heartbeat < QUEUE_HEARTBEAT_INTERVAL_SECS
    ):
        return
    _last_queue_heartbeat = now
    try:
        c = queue.heartbeat_counts()
    except sqlite3.Error as e:
        log.warning("queue heartbeat failed: %s", type(e).__name__)
        return
    d = queue.drain_deferrals()
    # ``suppressed_lines``: repeated indexer lines (embed retries and
    # recoveries, health-file and ingestion-state failures) the shared
    # rate limit withheld since the last heartbeat; the attachment
    # WARNINGs it withheld are in the attachments line instead.
    log.info(
        "queue: pending=%d retrying=%d deferred_permission=%d parked_trashed=%d dead=%d "
        "oldest_due_age=%ds; deferrals since last heartbeat: parse=%d embed=%d trashed=%d; "
        "suppressed_lines=%d",
        c["pending"],
        c["retrying"],
        c["deferred_permission"],
        c["parked_trashed"],
        c["dead"],
        c["oldest_due_age"],
        d[STAGE_PARSE],
        d[STAGE_EMBED],
        d[STAGE_TRASHED],
        drain_suppressed_lines(),
    )
    _log_reparse_progress(c["reparse"], c["reparse_dead"])


def _steady_state_summary_due(
    *, drained: int, drained_since_log: int, batch_size: int, seconds_since_summary: float
) -> bool:
    """Whether the steady-state loop logs its summaries now: every
    ``TIMING_LOG_EVERY`` drained messages; when a drain that followed
    drained work came back short of a full batch (the queue has no more
    ready jobs); or once ``SUMMARY_MAX_INTERVAL_SECS`` have passed."""
    if drained_since_log >= TIMING_LOG_EVERY:
        return True
    if drained_since_log and drained < batch_size:
        return True
    return seconds_since_summary >= SUMMARY_MAX_INTERVAL_SECS


def _iter_maildir_messages(root: Path):
    """Yield every message file under ``root`` whose parent is ``cur`` or
    ``new``, at any nesting depth. mbsync ``SubFolders Legacy`` writes
    ``Folders/.Clients/cur/msg`` — a flat ``iterdir`` over ``root`` would
    miss every nested folder's mail. ``os.walk`` includes the dot
    directories and, unlike ``rglob``, reports a directory it cannot
    read, so the walk ends with a WARNING counting them (#870): their
    mail stays unindexed until a later walk finds them readable. A
    directory that vanished mid-walk is gone, not unreadable."""
    unreadable = 0

    def _on_error(exc: OSError) -> None:
        nonlocal unreadable
        if not isinstance(exc, FileNotFoundError):
            unreadable += 1

    for dirpath, _dirnames, filenames in os.walk(root, onerror=_on_error):
        if os.path.basename(dirpath) not in ("cur", "new"):
            continue
        for name in filenames:
            filepath = Path(dirpath, name)
            if filepath.is_file():
                yield filepath
    if unreadable:
        log.warning(
            "Maildir walk: skipped %d director(ies) it could not read; "
            "their mail is not indexed until they are readable",
            unreadable,
        )


# Exception types whose text cannot carry mail content, so
# ``_stage_error`` keeps it. Kept deliberately small: every other type
# (email parser/generator errors, codec and charset-lookup errors,
# ``ValueError``, ``sqlite3.Error``, library errors) is reduced to its
# name, which costs debuggability but cannot leak a message.
# ``OSError`` is not listed: any library can raise it with free text,
# so ``_stage_error`` renders it from its errno alone.
_STAGE_ERROR_KEEP_TEXT: tuple[type[BaseException], ...] = (
    # Path and byte sizes only (parser.py).
    OversizedMessageError,
    # Fixed text plus counts by contract (embedder.py).
    EmbedResponseError,
)


def _stage_error(exc: BaseException) -> str:
    """Render a pipeline-stage exception for ``indexing_jobs.last_error``
    and the queue's retry / dead-letter log lines.

    Only types in ``_STAGE_ERROR_KEEP_TEXT`` keep their message; the
    rest are rendered as the type name alone, because their text can
    quote the message being indexed (``HeaderWriteError`` embeds the
    refused header, a codec error its data, a charset ``LookupError``
    the sender's label, and an FTS5 ``sqlite3.OperationalError`` its
    query term). Uses ``str()``, never ``repr()``: some exceptions carry
    their input as an attribute that only ``repr()`` shows.
    """
    if isinstance(exc, _STAGE_ERROR_KEEP_TEXT):
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        # The errno's fixed ``os.strerror`` text, never the exception's
        # own message or filename; the job row already holds the path.
        # ``os.strerror`` raises outside the C int range.
        try:
            reason = os.strerror(exc.errno)
        except OverflowError, ValueError:
            return type(exc).__name__
        return f"{type(exc).__name__}: [Errno {exc.errno}] {reason}"
    return type(exc).__name__


@dataclass
class _BatchedMsg:
    """Per-message state carried through the two-phase batched indexer.

    Phase 1 populates ``msg`` and ``thread`` and commits the thread
    membership with a seed thread vector chosen from a three-case
    priority chain: ``mean(existing chunk vectors)`` when the thread
    is already indexed with content; the prior ``threads_vec`` row
    when the thread is chunkless but has a non-zero embedding (covers
    subject-fallback threads); otherwise placeholder zero (a new
    thread, or a chunkless one whose stored vector is still zero after
    a crash between phases). Phase 2a populates the chunk + attachment-plan
    fields and the offsets that point each new chunk into the batch's
    flat embed-input list. Phase 2c reads the bulk-embedded vectors
    back through those offsets and applies the per-message DB writes,
    replacing the seed with the real mean-of-chunks (or
    subject-fallback) vector.
    """

    row: sqlite3.Row
    msg: Message
    thread: Thread
    body_chunks: list[MessageChunk] = field(default_factory=list)
    new_body_chunks: list[MessageChunk] = field(default_factory=list)
    new_body_offsets: list[int] = field(default_factory=list)
    attach_plans: list[AttachmentWritePlan] = field(default_factory=list)
    attach_new_chunks: list[list[MessageChunk]] = field(default_factory=list)
    attach_offsets: list[list[int]] = field(default_factory=list)
    # Offset of this message's subject-fallback text in the batch's flat
    # ``all_texts`` list, or ``None`` when no fallback is needed. The
    # fallback is added in Phase 2a only when the message contributes
    # zero new chunks AND its parent thread has no existing chunks —
    # i.e. exactly the case where the Phase 1 seed is the placeholder
    # zero. Without this fallback the thread vector would be
    # permanently stuck at zero (search quality regression). Mirrors
    # the old ``_seed_thread_embedding`` subject fallback.
    subject_fallback_offset: int | None = None
    # Phase 1 kept the thread's stored non-zero vector as its seed (the
    # thread has no chunks). A reparse then reuses it instead of
    # embedding the subject fallback again (#1078).
    kept_prior_vector: bool = False
    parse_ms: float = 0.0
    thread_ms: float = 0.0
    phase1_ms: float = 0.0
    chunk_ms: float = 0.0


# Unreadable-file handoff (see ``_phase1_commit_thread``): retry every
# minute, for up to a day after the job was enqueued.
PERMISSION_DEFER_SECS = 60
# How long a job for a T-flagged, still-indexed file is parked while
# deletion reconciliation decides its message (see ``_drain_queue_batched``).
TRASHED_DEFER_SECS = 60 * 60
PERMISSION_DEFER_WINDOW_SECS = 24 * 60 * 60
# A reparse job whose file vanished while its path is still indexed waits
# once, this long, for the watcher to record the rename (see
# ``_phase1_commit_thread``). A file still missing after that is really
# gone and is dropped with reason ``reparse_file_missing``.
REPARSE_RENAME_DEFER_SECS = 60
REPARSE_RENAME_DEFERRED_ERROR = "FileNotFoundError: deferred until the rename is recorded"


def _enqueued_within(row: sqlite3.Row, seconds: int) -> bool:
    created = datetime.fromisoformat(row["created_at"])
    return (datetime.now(UTC) - created).total_seconds() < seconds


def _phase1_commit_thread(
    row: sqlite3.Row,
    db: Database,
    threader: Threader,
    queue: IndexingQueue,
) -> _BatchedMsg | None:
    """Phase 1 of the batched indexer for one message.

    Parse → thread → ``upsert_thread`` with a seed vector chosen from
    a three-case priority chain: ``mean`` of the thread's existing
    chunk vectors when it has any; the prior non-zero ``threads_vec``
    row when the thread is chunkless (subject-fallback threads);
    otherwise the placeholder zero, for a new thread or a chunkless one
    whose stored vector is still zero (a retry after a crash between
    phases). Returns a populated ``_BatchedMsg`` on success. On any
    failure, marks the queue row appropriately and returns ``None`` so
    the caller skips the message without aborting the whole batch.
    """
    filepath = row["filepath"]

    t0 = time.perf_counter()
    try:
        msg = parse_email(Path(filepath), maildir_root=MAILDIR_PATH)
    except FileNotFoundError:
        # mbsync flag-rename race: file moved between enqueue and parse.
        # Watchdog's IN_MOVED_TO will re-enqueue under the new name.
        # Not a reparse of a path still indexed, though: ``on_moved``
        # only moves an indexed file's records (and its job, through
        # ``update_filepath``), so dropping the job before the rename is
        # recorded would lose the reparse. Wait for it once, without
        # spending an attempt (Codex round 1 on #1143); a file still
        # missing after that is gone and dropped.
        if (
            row["reason"] == REASON_REPARSE
            and row["last_error"] != REPARSE_RENAME_DEFERRED_ERROR
            and db.is_indexed(filepath)
        ):
            queue.defer(
                filepath,
                stage=STAGE_PARSE,
                error=REPARSE_RENAME_DEFERRED_ERROR,
                error_class=ERROR_CLASS_RETRYABLE,
                delay_seconds=REPARSE_RENAME_DEFER_SECS,
            )
            return None
        queue.mark_skipped(
            filepath,
            reason="reparse_file_missing" if row["reason"] == REASON_REPARSE else "file_missing",
        )
        return None
    except PermissionError as e:
        # mbsync ``chmod go+r``s new files only after its whole sync
        # finishes, so during a long sync a delivered file is still 0600
        # to this UID for longer than the retry budget. That is the
        # expected handoff, not the message's fault: defer without an
        # attempt. A fault that outlasts any sync falls through to the
        # normal retry path so it still ends in a visible dead row.
        if _enqueued_within(row, PERMISSION_DEFER_WINDOW_SECS):
            # Fixed deferral text, not ``_stage_error(e)``: the heartbeat
            # tells a deferral from a retry by it (#874).
            queue.defer(
                filepath,
                stage=STAGE_PARSE,
                error=PERMISSION_DEFERRED_ERROR,
                error_class=ERROR_CLASS_RETRYABLE,
                delay_seconds=PERMISSION_DEFER_SECS,
            )
        else:
            queue.mark_failed(filepath, stage="parse", error=_stage_error(e))
        return None
    except OversizedMessageError as e:
        # File exceeds INDEXER_PARSE_MAX_BYTES. Terminal under current
        # config — retrying will find the same oversized file and burn
        # embed budget. Dead-letter so the durable row survives restarts
        # (the file is still on disk, so deleting the row would let
        # ``initial_index`` re-enqueue it on every container start). The
        # ``is_dead`` gate then skips the file thereafter, and operators
        # see the entry in ``queue.stats()['dead']``.
        queue.mark_dead_terminal(filepath, stage="parse", error=f"oversized: {e}")
        return None
    except Exception as e:
        queue.mark_failed(filepath, stage="parse", error=_stage_error(e))
        return None
    parse_ms = (time.perf_counter() - t0) * 1000
    if msg is None:
        # Parser returned None for a terminal reason (no Message-ID, or
        # one over ``MESSAGE_ID_MAX_CHARS``).
        # Dead-letter rather than delete the row: the file is never
        # written to ``indexed_files``, so a deleted row would let every
        # Maildir walk re-enqueue and re-parse it forever. The dead row
        # makes the walk skip it and keeps it visible in queue stats.
        queue.mark_dead_terminal(
            filepath, stage="parse", error="unindexable: no Message-ID or one over 998 characters"
        )
        return None

    t0 = time.perf_counter()
    try:
        # Before threading, so the thread range, messages row and chunk
        # dates all see the same date.
        db.keep_persisted_fallback_date(msg)
        thread = threader.assign_thread(msg)
    except Exception as e:
        queue.mark_failed(filepath, stage="thread", error=_stage_error(e))
        return None
    thread_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    # Seed the Phase 1 thread vector. Three cases, in priority order:
    #   1. Thread has chunk vectors — use their mean. This is the
    #      canonical seed for already-indexed threads with content.
    #   2. No chunk vectors but a prior threads_vec row exists with
    #      a non-zero embedding — preserve that. Covers chunkless
    #      threads whose vector came from a subject fallback (an
    #      earlier blank-body message in the same thread). Without
    #      this branch, a new sibling message's Phase 1 commit would
    #      clobber the subject vector with zero, and a Phase 2
    #      failure or dead-letter would leave it permanently zero.
    #   3. Neither — truly new thread, or existing thread with a
    #      zero-vector row from a prior crashed batch. Use the
    #      placeholder zero; Phase 2c will replace it with either
    #      mean-of-new-chunks or the subject fallback.
    # ``get_phase1_seed_state`` short-circuits with empty/None for
    # brand-new threads via a cheap PK existence check, so the bulk
    # case during an initial scan does one PK lookup instead of two
    # empty reads.
    existing_chunk_embs, prior_vec = db.get_phase1_seed_state(thread.thread_id)
    kept_prior_vector = False
    if existing_chunk_embs:
        seed_vector = mean_vector(existing_chunk_embs)
    elif prior_vec is not None and any(v != 0.0 for v in prior_vec):
        seed_vector = prior_vec
        kept_prior_vector = True
    else:
        seed_vector = _ZERO_THREAD_VECTOR
    try:
        # Phase 1 commit: write thread + message_thread_map + indexed_files
        # with the seed vector. Threading state is durable before
        # Phase 2 runs, so the next message in the batch sees this
        # message's thread when computing its own thread assignment.
        db.upsert_thread(thread, seed_vector)
    except Exception as e:
        queue.mark_failed(filepath, stage="thread_commit", error=_stage_error(e))
        return None
    phase1_ms = (time.perf_counter() - t0) * 1000

    return _BatchedMsg(
        row=row,
        msg=msg,
        thread=thread,
        parse_ms=parse_ms,
        thread_ms=thread_ms,
        phase1_ms=phase1_ms,
        kept_prior_vector=kept_prior_vector,
    )


def _chunk_embed_input(subject_line: str, chunk_text: str) -> str:
    """Embedding input for a message's first body chunk: ``subject_line``
    then the chunk text, within ``CHUNK_MAX_TOKENS`` (#439).

    The subject is attacker-controlled and unbounded, so it is cut to
    the tokens the chunk leaves free. When the chunk is already at the
    ceiling the prefix is dropped and the chunk is embedded as stored.
    The subject is first cut to 16 characters per budget token, so a
    huge subject is never tokenized whole; real tokens are shorter than
    that, so the cut does not shorten what fits.
    """
    # Two tokens of slack for the "\n\n" separator and any merge at
    # the join; the final count below is the actual guard.
    budget = CHUNK_MAX_TOKENS - estimate_tokens(chunk_text) - 2
    if budget <= 0:
        return chunk_text
    prefix = truncate_to_tokens(subject_line[: budget * 16], budget)
    if not prefix.strip():
        return chunk_text
    combined = f"{prefix}\n\n{chunk_text}"
    if estimate_tokens(combined) > CHUNK_MAX_TOKENS:
        return chunk_text
    return combined


def _phase2a_collect_chunks(
    state: _BatchedMsg,
    db: Database,
    all_texts: list[str],
    *,
    progress: Callable[[], None] = lambda: None,
    batch_extractions: dict[tuple[str, str], ExtractionResult] | None = None,
) -> tuple[bool, str | None]:
    """Phase 2a: chunk the body and attachments WITHOUT embedding.

    Appends every new chunk's text to the shared ``all_texts`` list and
    records the offsets on ``state`` so Phase 2c can read its vectors
    back. ``batch_extractions`` carries the batch's uncommitted
    extraction results, so identical bytes are extracted once per batch
    and extractor module (#237, #928). Returns ``(True, None)`` on success or ``(False, error)`` on
    a chunk/extract failure (rare — usually only attachment OCR errors)
    so the caller can mark the queue row failed without aborting the
    rest of the batch.

    Subject fallback gating depends ONLY on COMMITTED state — the
    earlier-shape "skip fallback if an earlier batch sibling queued
    chunks for the same thread" optimization conflated pending and
    committed work: if that earlier sibling's Phase 2c then failed,
    the chunkless successor would commit cleanly with no chunks, no
    fallback, and a thread vector stuck at the Phase 1 zero
    placeholder (operator-invisible retrieval-quality regression).
    Reserving an extra embed-batch slot per chunkless sibling on a
    chunk-bearing thread costs one BPE encode and one embed payload
    entry — negligible compared to the bulk-embed savings, and the
    correctness story is straightforward: fallback is reserved iff
    the message has no new chunks AND either the thread has no
    committed chunks at the moment of the check or this message's
    Phase 2c will delete chunks it committed earlier (which may be the
    thread's last ones).
    """
    msg = state.msg
    t0 = time.perf_counter()
    try:
        # Segment before chunking so no chunk spans kinds (#646).
        body_chunks = chunk_segments(
            message_pk=msg.claimant_id,
            segments=[(s.kind, s.text) for s in segment_for_embedding(msg.body_text or "")],
            target_tokens=CHUNK_TARGET_TOKENS,
            max_tokens=CHUNK_MAX_TOKENS,
            overlap_tokens=CHUNK_OVERLAP_TOKENS,
        )
        stored_ids = db.get_chunk_ids_for_message(msg.claimant_id)
        new_body = [c for c in body_chunks if c.chunk_id not in stored_ids]
        # Whether Phase 2c will delete chunks this message committed
        # earlier without adding any (a body or a re-extracted
        # attachment that now yields no text). The thread may then end
        # up chunkless with a vector still seeded from the deleted
        # chunks, so the fallback below is reserved for that case too.
        clears_chunks = bool(stored_ids) and not body_chunks
        new_body_offsets: list[int] = []
        # Every message carries its subject into the embedding input of
        # its first body chunk only (#303, #687). The stored chunk
        # text, offsets and ID stay body-only, since chunks are the
        # authoritative body store; keyword search gets the subject from
        # the thread's FTS subject column instead.
        subject_line = subject_embed_line(msg)
        for c in new_body:
            new_body_offsets.append(len(all_texts))
            if subject_line and c.chunk_index == 0:
                all_texts.append(_chunk_embed_input(subject_line, c.text))
            else:
                all_texts.append(c.text)

        attach_plans: list[AttachmentWritePlan] = []
        attach_new_chunks: list[list[MessageChunk]] = []
        attach_offsets: list[list[int]] = []
        # Copies of the same bytes in one message chunk to the same chunk
        # IDs (the chunk key is message + content hash): embed each once.
        queued_attach_offsets: dict[str, int] = {}
        attach_stored_ids: list[set[str]] = []
        # The first plan with text for each payload in this message. The
        # same bytes under labels that run different extractors give
        # different texts (#928), but share one chunk slice: the first
        # plan writes every text's chunks, so none replaces another.
        slice_holder: dict[str, int] = {}
        if INDEXER_ATTACHMENT_EXTRACTION_ENABLED and msg.attachments:
            cap = (
                INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS
                if INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS > 0
                else None
            )
            for occurrence_index, attachment in enumerate(msg.attachments):
                # Each attachment's extraction is separately bounded
                # (byte caps, OCR page cap and timeouts), so it is the
                # unit the stall guard's limit applies to.
                progress()
                # The plan comes back with empty embeddings_by_chunk_id;
                # the stored-ID diff below picks its new chunks and
                # Phase 2c fills it from the batched embed result.
                plan = prepare_attachment_writes(
                    attachment=attachment,
                    claimant_id=msg.claimant_id,
                    db=db,
                    chunk_target_tokens=CHUNK_TARGET_TOKENS,
                    chunk_max_tokens=CHUNK_MAX_TOKENS,
                    chunk_overlap_tokens=CHUNK_OVERLAP_TOKENS,
                    ocr_enabled=INDEXER_OCR_ENABLED,
                    max_bytes=INDEXER_ATTACHMENT_MAX_BYTES,
                    max_ocr_pages=INDEXER_OCR_MAX_PAGES,
                    ocr_timeout_seconds=INDEXER_OCR_TIMEOUT_SECONDS or None,
                    max_pdf_pages=INDEXER_PDF_MAX_DIGITAL_PAGES or None,
                    occurrence_index=occurrence_index,
                    max_extracted_chars=cap,
                    batch_extractions=batch_extractions,
                    # Each page read refreshes the heartbeat, so a long
                    # OCR does not read as unhealthy (#485). It does not
                    # restart the stall guard's clock, which stays per
                    # attachment.
                    on_progress=_extraction_heartbeat,
                )
                stored_attach_ids = db.get_chunk_ids_for_message(
                    msg.claimant_id, attachment_id=attachment.content_hash
                )
                holder = slice_holder.get(attachment.content_hash)
                if plan.chunks and holder is not None:
                    target = attach_plans[holder]
                    known = {c.chunk_id for c in target.chunks}
                    extra = [c for c in plan.chunks if c.chunk_id not in known]
                    target.chunks.extend(extra)
                    for c in extra:
                        if c.chunk_id in stored_attach_ids:
                            continue
                        if c.chunk_id not in queued_attach_offsets:
                            queued_attach_offsets[c.chunk_id] = len(all_texts)
                            all_texts.append(c.text)
                        attach_new_chunks[holder].append(c)
                        attach_offsets[holder].append(queued_attach_offsets[c.chunk_id])
                    plan.chunks = []
                elif plan.chunks:
                    slice_holder[attachment.content_hash] = len(attach_plans)
                plan_new = [c for c in plan.chunks if c.chunk_id not in stored_attach_ids]
                plan_offsets: list[int] = []
                for c in plan_new:
                    if c.chunk_id not in queued_attach_offsets:
                        queued_attach_offsets[c.chunk_id] = len(all_texts)
                        all_texts.append(c.text)
                    plan_offsets.append(queued_attach_offsets[c.chunk_id])
                attach_plans.append(plan)
                attach_new_chunks.append(plan_new)
                attach_offsets.append(plan_offsets)
                attach_stored_ids.append(stored_attach_ids)
        # A plan without text clears its attachment's chunk slice in
        # Phase 2c, unless another copy of the same bytes in this message
        # fills it: that copy counted the stored chunks as kept and
        # embedded none of them, so it could not restore a cleared slice.
        filled = {plan.attachment.content_hash for plan in attach_plans if plan.chunks}
        for plan, stored_attach_ids in zip(attach_plans, attach_stored_ids):
            if plan.chunks:
                continue
            if plan.attachment.content_hash in filled:
                plan.clears_stale_chunks = False
            else:
                clears_chunks = clears_chunks or bool(stored_attach_ids)
        # Subject-fallback path: when this message contributes zero new
        # chunks AND the parent thread has no committed chunks, embed
        # the subject (or a sentinel string) so the thread vector is
        # not permanently stuck at the Phase 1 placeholder zero.
        # Mirrors the old ``_seed_thread_embedding`` per-message
        # behavior. The fallback text rides through Phase 2b inside the
        # same batched embed call as everyone else's chunks.
        #
        # The check is on COMMITTED chunks only (``thread_has_chunks``),
        # not on pending in-batch chunks. An earlier batch sibling that
        # queued chunks for the same thread but whose Phase 2c then
        # fails would otherwise leave this chunkless successor
        # committing cleanly with a zero thread vector — see the
        # docstring rationale above. Reserving an extra fallback slot
        # when an earlier sibling also produced chunks is harmless:
        # Phase 2c's three-case priority chain prefers
        # ``mean(committed chunks)`` over the fallback embedding, so
        # the fallback only takes effect when nothing else committed.
        #
        # ``thread_has_chunks`` is a single-row existence check —
        # avoids the per-message blob-unpack churn that
        # ``get_thread_chunk_embeddings`` would impose on chatty
        # threads where this gate fires for every chunkless arrival.
        has_new_chunks = bool(new_body) or any(plan_new for plan_new in attach_new_chunks)
        # A reparse changes no chunk text or embedding input (that would
        # be a rebuild; docs/architecture.md, "Reparse in place"), so a
        # chunkless thread's stored subject-fallback vector is still the
        # one this fallback would embed: keep it and make no call
        # (#1078). A vector still at the zero placeholder is repaired.
        keeps_vector = (
            state.row["reason"] == REASON_REPARSE and state.kept_prior_vector and not clears_chunks
        )
        if (
            not has_new_chunks
            and not keeps_vector
            and (clears_chunks or not db.thread_has_chunks(state.thread.thread_id))
        ):
            # Source the fallback text from the thread's stored
            # ``display_subject`` rather than from ``state.msg.subject``.
            # ``display_subject`` is the OLDEST message's original-case
            # subject, maintained by ``upsert_thread``'s merge across
            # every arrival. Reading from there keeps the fallback
            # vector STABLE across successive chunkless arrivals: using
            # ``state.msg.subject`` made each new chunkless message
            # overwrite the prior thread vector with its own subject
            # embedding (msg1's "Quarterly Review" → msg2's "Re: Quarterly
            # Review" → msg3's "Fwd: ..."), producing arrival-order-
            # dependent thread vectors on a chunkless thread.
            #
            # Phase 1 already committed this message's row via
            # ``upsert_thread``, so the display_subject reflecting the
            # oldest-seen message is in the DB before this read. Falls
            # through to the message's own subject while display_subject
            # is NULL.
            stored_display = db.get_thread_display_subject(state.thread.thread_id)
            fallback_text = (stored_display or state.msg.subject or "").strip()
            if not fallback_text:
                fallback_text = "(empty thread)"
            state.subject_fallback_offset = len(all_texts)
            all_texts.append(fallback_text)
    except MemoryError, RecursionError:
        # Host pressure, which the extraction dispatcher re-raises: let it
        # escape the step with its ``begin_attempt`` charge held, as for a
        # process that dies mid-step, rather than spend the attempt as an
        # ordinary ``chunk`` failure.
        raise
    except Exception as e:
        # Phase 2a is "extract + chunk" only — no DB writes. A failure
        # here marks this message failed but leaves Phase 1's thread
        # commit in place (keyword-searchable but vectorless until
        # retry succeeds or the operator dead-letters this row).
        return False, _stage_error(e)

    state.body_chunks = body_chunks
    state.new_body_chunks = new_body
    state.new_body_offsets = new_body_offsets
    state.attach_plans = attach_plans
    state.attach_new_chunks = attach_new_chunks
    state.attach_offsets = attach_offsets
    state.chunk_ms = (time.perf_counter() - t0) * 1000
    return True, None


def _phase2c_commit_vectors(
    state: _BatchedMsg,
    db: Database,
    vectors: list[list[float]],
) -> tuple[bool, str | None]:
    """Phase 2c: per-message DB transaction for body + attachments + thread vec.

    Reads vectors out of the shared batch result via the offsets
    captured in Phase 2a, then writes everything inside one
    ``with db.transaction()`` block so chunks/vectors/thread-vector
    either all land or all roll back for this message.
    """
    msg = state.msg
    thread = state.thread

    body_embs = {
        c.chunk_id: vectors[i] for c, i in zip(state.new_body_chunks, state.new_body_offsets)
    }
    for plan, plan_new, plan_offsets in zip(
        state.attach_plans, state.attach_new_chunks, state.attach_offsets
    ):
        plan.embeddings_by_chunk_id = {
            c.chunk_id: vectors[i] for c, i in zip(plan_new, plan_offsets)
        }

    try:
        with db.transaction():
            db.replace_message_chunks(
                claimant_id=msg.claimant_id,
                thread_id=thread.thread_id,
                chunks=state.body_chunks,
                embeddings_by_chunk_id=body_embs,
            )
            for plan in state.attach_plans:
                apply_attachment_writes(
                    plan=plan,
                    claimant_id=msg.claimant_id,
                    thread_id=thread.thread_id,
                    db=db,
                )
            # Replace the Phase 1 seed thread vector. Three cases
            # mirror the old ``_seed_thread_embedding`` logic:
            #   1. Thread now has chunks (this message contributed
            #      some, or earlier messages already had them) → use
            #      the mean of those chunk vectors.
            #   2. No chunks anywhere on the thread, but Phase 2a
            #      reserved a subject-fallback slot in the embed batch
            #      (blank-body, no-attachment-chunks message — would
            #      otherwise be permanently stuck at zero) → use the
            #      subject-embedded vector.
            #   3. Neither — leave the placeholder zero in place.
            #      Should not occur in practice; if it does, the next
            #      message on the thread will overwrite via case 1.
            chunk_embs = db.get_thread_chunk_embeddings(thread.thread_id)
            if chunk_embs:
                db.replace_thread_vector(thread.thread_id, mean_vector(chunk_embs))
            elif state.subject_fallback_offset is not None:
                db.replace_thread_vector(thread.thread_id, vectors[state.subject_fallback_offset])
    except Exception as e:
        return False, _stage_error(e)
    # Counted once committed, so a message prepared again after an
    # embedder outage is counted once (review round 1 on #884).
    record_committed_outcomes(state.attach_plans)
    return True, None


class _EmbedOutageBreaker:
    """Pauses queue draining while the embedder is unavailable.

    Without it, every due batch during an outage would hit the dead
    embedder in turn (each call spending tenacity's in-call retries)
    and churn the whole queue. When a batch fails AND a probe confirms
    the embedder itself is down, the breaker opens: draining stops
    until the backoff elapses, then one batch tests the embedder again.
    The backoff doubles per consecutive outage up to ``cap_seconds``
    and resets on the first successful embed, which logs how long
    indexing was paused (#873).

    Pausing also skips Phase 1, so mail arriving during an outage is
    not keyword-searchable until the embedder returns. That is the
    price of not hammering a down provider; the queue keeps every job.
    """

    def __init__(self, base_seconds: float = 30, cap_seconds: float = 600):
        self.base_seconds = base_seconds
        self.cap_seconds = cap_seconds
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.outage_started = 0.0

    def allow(self, now: float) -> bool:
        return now >= self.open_until

    def record_failure(self, now: float) -> float:
        """Open the breaker; returns the pause length in seconds."""
        if not self.consecutive_failures:
            self.outage_started = now
        self.consecutive_failures += 1
        delay = min(
            self.base_seconds * (2 ** (self.consecutive_failures - 1)),
            self.cap_seconds,
        )
        self.open_until = now + delay
        return delay

    def record_success(self, now: float | None = None) -> None:
        if self.consecutive_failures:
            paused = (time.monotonic() if now is None else now) - self.outage_started
            log.info(
                "embedder recovered after %d failure(s), paused %ds; indexing resumed",
                self.consecutive_failures,
                paused,
            )
        self.consecutive_failures = 0
        self.open_until = 0.0


# Deferral delay for an outage when no breaker is supplied (compat
# shims and tests). Production always passes the main-loop breaker.
_OUTAGE_DEFER_SECONDS = 30.0

_EMBED_PROBE_TEXT = "embedder health probe"


def _probe_embedder(embedder: EmbeddingBackend) -> BaseException | None:
    """Embed a tiny known-good input. Returns the error if the embedder
    cannot embed anything (outage or misconfiguration), else ``None``."""
    # The failure that prompted this probe may have run a full retry
    # cycle; refresh the heartbeat so the probe's own cycle starts fresh.
    touch_health_file()
    try:
        embedder.embed(_EMBED_PROBE_TEXT)
    except Exception as e:
        return e
    return None


def _entry_text_offsets(entry: _BatchedMsg) -> list[int]:
    """Every index into the batch's flat embed-input list that belongs
    to ``entry`` — body chunks, attachment chunks, subject fallback —
    each once (copies of one attachment share their chunks' offsets)."""
    offsets = list(entry.new_body_offsets)
    for plan_offsets in entry.attach_offsets:
        offsets.extend(plan_offsets)
    if entry.subject_fallback_offset is not None:
        offsets.append(entry.subject_fallback_offset)
    return list(dict.fromkeys(offsets))


def _pause_embedding(
    entries: list[_BatchedMsg],
    queue: IndexingQueue,
    breaker: _EmbedOutageBreaker | None,
    exc: BaseException,
) -> None:
    """The embedder itself is failing: defer ``entries`` without
    spending attempts and open the breaker so draining stops.

    A configuration error (or a probe the provider refuses outright)
    records ``operator_action_required``; anything else is an outage
    and records ``retryable``.
    """
    kind = classify_embed_failure(exc)
    error_class = (
        ERROR_CLASS_OPERATOR
        if kind in (EMBED_FAILURE_CONFIGURATION, EMBED_FAILURE_REJECTED_INPUT)
        else ERROR_CLASS_RETRYABLE
    )
    err_repr = scrub_embed_error(exc)
    delay = (
        breaker.record_failure(time.monotonic()) if breaker is not None else _OUTAGE_DEFER_SECONDS
    )
    for entry in entries:
        queue.defer(
            entry.row["filepath"],
            stage=STAGE_EMBED,
            error=err_repr,
            error_class=error_class,
            delay_seconds=delay,
        )
    if error_class == ERROR_CLASS_OPERATOR:
        log.error(
            "embedder rejected credentials or model (%s) — check EMBED_BASE_URL, "
            "EMBED_MODEL and the embed API key. Deferred %d message(s); "
            "indexing paused %ds.",
            err_repr,
            len(entries),
            delay,
        )
    else:
        log.error(
            "embedder unavailable (%s). Deferred %d message(s) without "
            "spending retry attempts; indexing paused %ds.",
            err_repr,
            len(entries),
            delay,
        )


def _embed_progress(queue: IndexingQueue) -> Callable[[], None]:
    """Callback for each completed embed request: refresh the heartbeat
    and restart the stall guard's clock. A lone survivor stays charged,
    and watched, through the bulk embed; each request is separately
    bounded, so it is the unit the guard's limit applies to."""

    def progress() -> None:
        touch_health_file()
        queue.note_progress()

    return progress


def _embed_message_texts(
    texts: list[str],
    embedder: EmbeddingBackend,
    on_progress: Callable[[], None] | None = None,
) -> list[list[float]]:
    """Embed one message's texts, telling a request limit from bad content.

    ``embed_batch`` packs up to ``EMBED_BATCH_SIZE`` texts into each HTTP
    request. A provider or gateway that rejects such a request (413, or
    a 400 / 422 batch limit) has only shown that the *request* was
    unacceptable, not that any of the message's content is. So a
    rejected multi-input request is retried one text per request; only
    a rejection of a single text propagates as evidence against the
    source. Every other failure propagates unchanged for the caller to
    attribute.
    """
    if on_progress is None:
        on_progress = touch_health_file
    try:
        return embedder.embed_batch(texts, on_batch_complete=on_progress)
    except Exception as e:
        if len(texts) <= 1 or classify_embed_failure(e) != EMBED_FAILURE_REJECTED_INPUT:
            raise
        log.warning(
            "embed request with %d inputs rejected (%s); retrying one input per "
            "request. If this recurs, the provider's request limit is below "
            "EMBED_BATCH_SIZE — lower it.",
            len(texts),
            scrub_embed_error(e),
        )
    return [embedder.embed_batch([t], on_batch_complete=on_progress)[0] for t in texts]


def _embed_each_message(
    survivors: list[_BatchedMsg],
    all_texts: list[str],
    embedder: EmbeddingBackend,
    queue: IndexingQueue,
    breaker: _EmbedOutageBreaker | None,
) -> tuple[list[list[float]], list[_BatchedMsg], bool]:
    """Embed each message's texts on its own after a failed batch embed.

    Called once a probe has shown the embedder can embed *something*.
    That probe describes one tiny request, not the provider's state for
    every later request, so each individual failure is attributed with
    ``classify_embed_failure`` before any message is charged:

    * ``rejected_input`` (400 / 413 / 422) on a single text — the
      provider refused this message's content: ``mark_dead_terminal``
      as a permanent source failure. The only terminal outcome. (A
      rejected multi-text request is first split into one text per
      request by ``_embed_message_texts``, so a request-size limit is
      never charged to the source.)
    * ``infrastructure`` / ``configuration`` (transport, 408, 429,
      401 / 403 / 404) — not the message's fault: this and every
      remaining message are deferred without spending attempts, the
      breaker opens, and isolation stops.
    * ``uncertain`` (5xx, integrity errors) — probe again. A failing
      probe means the provider went down: pause as above. A passing
      probe points at this input: ``mark_failed`` spends one attempt,
      so a genuinely poison input still dead-letters eventually without
      stalling the queue behind the breaker.

    Returns a vector list aligned to ``all_texts`` (slots of failed
    messages stay empty and are never read), the entries that embedded
    cleanly, and whether embedding was paused.
    """
    vectors: list[list[float]] = [[] for _ in all_texts]
    ok: list[_BatchedMsg] = []
    for index, entry in enumerate(survivors):
        touch_health_file()
        offsets = _entry_text_offsets(entry)
        filepath = entry.row["filepath"]
        try:
            entry_vectors = (
                _embed_message_texts(
                    [all_texts[i] for i in offsets], embedder, _embed_progress(queue)
                )
                if offsets
                else []
            )
        except Exception as e:
            failure: BaseException = e
            kind = classify_embed_failure(e)
            if kind == EMBED_FAILURE_UNCERTAIN:
                probe_error = _probe_embedder(embedder)
                if probe_error is None:
                    queue.mark_failed(filepath, stage="embed", error=scrub_embed_error(e))
                    continue
                failure = probe_error
            elif kind == EMBED_FAILURE_REJECTED_INPUT:
                queue.mark_dead_terminal(filepath, stage="embed", error=scrub_embed_error(e))
                continue
            _pause_embedding(survivors[index:], queue, breaker, failure)
            return vectors, ok, True
        for i, vector in zip(offsets, entry_vectors):
            vectors[i] = vector
        ok.append(entry)
        touch_health_file()
    return vectors, ok, False


def _drain_queue_batched(
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
    queue: IndexingQueue,
    *,
    batch_size: int,
    timing_aggregator: TimingAggregator,
    max_passes: int | None = None,
    breaker: _EmbedOutageBreaker | None = None,
    skip_trashed: bool = False,
    summary_every: int | None = None,
) -> int:
    """Drain the queue in two-phase batches.

    Phase 1 commits thread membership per-message with a seed thread
    vector — ``mean(existing chunk vectors)`` for threads with chunks,
    the prior non-zero ``threads_vec`` row for chunkless ones,
    placeholder zero for new ones and for chunkless ones whose stored
    vector is still zero after a crash — so (a) the next message
    in the batch's threader can see this message's thread, and (b) a
    Phase 2 failure cannot regress an already-good thread vector to
    zero.
    Phase 2a chunks the body + attachment text without embedding.
    Phase 2b issues a single ``embed_batch`` covering every new chunk
    across the whole batch — this is the optimization: ~25k single-
    HTTP-call messages becomes ~500 multi-message HTTP calls against a
    cloud embedder.
    Phase 2c commits the chunk + vector writes per message and
    overwrites the Phase 1 seed thread vector with the real
    mean-of-chunks vector.

    ``max_passes`` bounds how many ``claim_batch`` rounds run before
    returning. ``None`` (the default) drains until the queue is empty
    — the right shape for ``initial_index``. Steady-state callers in
    the main loop pass ``max_passes=1`` so each tick interleaves
    cleanly with the reconciler sweep, WAL checkpoint, and health-file
    refresh instead of starving them on a long burst.

    ``summary_every`` (``initial_index`` passes ``TIMING_LOG_EVERY``)
    logs the timing summary and the attachments aggregate after each
    batch that brings the messages since the last summary to at least
    that many. ``None`` logs nothing here: the steady-state loop logs
    them itself (``_steady_state_summary_due``).

    ``skip_trashed`` (set whenever deletion reconciliation is enabled;
    see ``_enqueue_unindexed_messages``) never indexes a claimed job
    whose file is T-flagged: the reconciler owns that message now, and
    indexing it would outlive its reap. The job of a message still in
    the index is parked without spending an attempt: the reap deletes it
    with the message's rows, or, if mbsync clears the flag first, the
    rename moves it to the live path where it runs. A trashed file that
    was never indexed has nothing to keep, so its job is dropped.

    Failure isolation:

    * Phase 1 error for one message — that message marked failed,
      others continue.
    * Phase 2a (chunk/extract) error for one message — marked failed,
      Phase 1's commit stays. Vector-less but text-searchable.
    * Phase 2b (embed) error — a probe decides whose fault it is.
      Embedder down or misconfigured: every in-flight row is deferred
      without spending attempts, ``breaker`` opens, and this call
      stops draining. Embedder healthy: each message is re-embedded on
      its own, so one bad input cannot fail its batchmates; each
      individual failure is attributed before anyone is charged (see
      ``_embed_each_message``). Phase 1 commits remain either way; a
      later pass re-runs Phase 1 (idempotent upsert) plus Phase 2.
    * Phase 2c (DB write) error for one message — marked failed,
      others succeed.
    """
    processed = 0
    summarized = 0
    passes = 0
    while True:
        if max_passes is not None and passes >= max_passes:
            break
        if breaker is not None and not breaker.allow(time.monotonic()):
            break
        passes += 1
        _maybe_log_queue_heartbeat(queue)
        # ---- Gather batch + Phase 1 ----
        # Snapshot up to batch_size distinct queued rows in one query
        # so the gather loop cannot re-claim the same row repeatedly
        # while we defer mark_succeeded to Phase 2c.
        rows = queue.claim_batch(batch_size)
        if not rows:
            break
        # A reparse the heartbeat never saw queued (drained between two
        # heartbeats) still gets its completion line (Codex round 1 on #1143).
        if any(row["reason"] == REASON_REPARSE for row in rows):
            _reparse_progress.active = True
        # A row still marked ``interrupted`` was mid-step when the
        # indexer died. An out-of-memory kill can come from the whole
        # batch's footprint rather than that message, and a restart
        # replays the same batch in the same order, so the row runs
        # alone first: only a message that fails on its own keeps
        # accumulating charges toward ``dead``.
        interrupted = [row for row in rows if row["last_stage"] == INTERRUPTED_STAGE]
        if interrupted and len(rows) > 1:
            rows = interrupted[:1]
        batch: list[_BatchedMsg] = []
        for row in rows:
            if skip_trashed and is_trashed(row["filepath"]):
                if db.find_message_entry_by_filepath(row["filepath"]) is None:
                    queue.mark_skipped(row["filepath"], reason="trashed")
                else:
                    queue.defer(
                        row["filepath"],
                        stage=STAGE_TRASHED,
                        error="file is T-flagged; parked until reaped or restored",
                        error_class=ERROR_CLASS_RETRYABLE,
                        delay_seconds=TRASHED_DEFER_SECS,
                    )
                continue
            # Parse and extraction are the steps hostile input can crash
            # or hang, so each runs with its message charged one attempt
            # (see ``IndexingQueue.begin_attempt``). The refund is not in
            # a ``finally``: an exception escaping the step takes the
            # process down, and the charge must survive that.
            if not queue.begin_attempt(row["filepath"]):
                continue
            entry = _phase1_commit_thread(row, db, threader, queue)
            queue.end_attempt(row["filepath"])
            processed += 1
            if entry is not None:
                batch.append(entry)
            touch_health_file()

        if not batch:
            continue

        # ---- Phase 2a: collect chunks across batch (no embed) ----
        # Each entry can take seconds to many tens of seconds for a
        # large attachment-heavy message (PDF chunking, OCR, etc.). The
        # cumulative wall-clock for a batch_size=50 batch can blow past
        # HEALTH_MAX_AGE_SECONDS, so touch the heartbeat after every
        # entry — not just before/after the bulk embed.
        all_texts: list[str] = []
        survivors: list[_BatchedMsg] = []
        batch_extractions: dict[tuple[str, str], ExtractionResult] = {}
        for entry in batch:
            if not queue.begin_attempt(entry.row["filepath"]):
                continue
            ok, err = _phase2a_collect_chunks(
                entry,
                db,
                all_texts,
                progress=queue.note_progress,
                batch_extractions=batch_extractions,
            )
            queue.end_attempt(entry.row["filepath"])
            if ok:
                survivors.append(entry)
            else:
                queue.mark_failed(entry.row["filepath"], stage="chunk", error=err or "")
            touch_health_file()

        if not survivors:
            continue

        # The bulk embed and the vector commits hold the whole batch's
        # vectors, so a kill there cannot be pinned on one message.
        # Several survivors are marked ``interrupted`` without a charge,
        # which replays each alone after a restart; a lone survivor —
        # already running alone — stays charged until its outcome is
        # recorded, so one that dies even alone still reaches ``dead``.
        if len(survivors) == 1:
            if not queue.begin_attempt(survivors[0].row["filepath"]):
                continue
        else:
            queue.mark_interrupted([entry.row["filepath"] for entry in survivors])

        # Refresh the heartbeat just before the bulk embed so a slow
        # cloud-embedder round-trip (potentially tens of seconds for a
        # full batch of chunks) doesn't age the health file past
        # HEALTH_MAX_AGE_SECONDS while no per-message touch fires.
        touch_health_file()

        # ---- Phase 2b: bulk embed across batch ----
        t_embed_start = time.perf_counter()
        paused = False
        try:
            vectors = (
                embedder.embed_batch(all_texts, on_batch_complete=_embed_progress(queue))
                if all_texts
                else []
            )
        except Exception as e:
            # Scrub the error before persistence: ``APIStatusError`` can
            # echo input fragments (email body text) on 4xx, and
            # ``last_error`` rides into ``indexing_jobs.last_error`` +
            # operator log sinks. ``scrub_embed_error`` keeps full repr
            # for safe error shapes (connection / timeout / our own
            # ``EmbedResponseError``), trims SDK status errors to
            # type + status_code, and anything else to its type.
            err_repr = scrub_embed_error(e)
            probe_error = _probe_embedder(embedder)
            if probe_error is not None:
                _pause_embedding(survivors, queue, breaker, probe_error)
                break
            log.warning(
                "batched embed failed (%s) but the embedder is healthy; "
                "embedding %d message(s) individually to isolate the bad input.",
                err_repr,
                len(survivors),
            )
            vectors, survivors, paused = _embed_each_message(
                survivors, all_texts, embedder, queue, breaker
            )
        # A batch with nothing to embed sent no request, so it proves
        # nothing about the provider: it neither closes the breaker nor
        # logs its recovery (Codex round 2 on #904).
        if breaker is not None and not paused and all_texts:
            breaker.record_success()
        embed_ms = (time.perf_counter() - t_embed_start) * 1000
        # Attribute embed time evenly across the batch for telemetry.
        per_msg_embed_ms = embed_ms / max(1, len(survivors))

        # ---- Phase 2c: per-message vector commits ----
        for entry in survivors:
            t0 = time.perf_counter()
            ok, err = _phase2c_commit_vectors(entry, db, vectors)
            db_write_ms = (time.perf_counter() - t0) * 1000
            if ok:
                queue.mark_succeeded(entry.row["filepath"])
                if entry.row["reason"] == REASON_REPARSE:
                    _reparse_progress.reparsed += 1
                # ``db_write_ms`` aggregates BOTH DB-write phases:
                # Phase 1's ``upsert_thread`` (recorded as
                # ``entry.phase1_ms``) plus the Phase 2c per-message
                # transaction (``db_write_ms`` measured above). Without
                # the Phase 1 contribution the ``db_write`` and
                # ``total`` columns in the periodic summary
                # under-report wall-clock — Phase 1 commits are
                # idempotent upserts but still cost SQLite IO time.
                timing_aggregator.record(
                    StageTimings(
                        parse_ms=entry.parse_ms,
                        thread_ms=entry.thread_ms,
                        chunk_ms=entry.chunk_ms,
                        embed_ms=per_msg_embed_ms,
                        db_write_ms=entry.phase1_ms + db_write_ms,
                    )
                )
            else:
                queue.mark_failed(entry.row["filepath"], stage="db_write", error=err or "")
            touch_health_file()

        if paused:
            # Messages embedded before the pause were committed above;
            # the rest are deferred and the breaker is open.
            break

        if summary_every is not None and processed - summarized >= summary_every:
            line = format_summary(timing_aggregator.summary())
            if line:
                log.info(line)
            _log_attachment_outcomes()
            summarized = processed

    return processed


def _recover_zero_vector_threads(
    db: Database,
    queue: IndexingQueue,
    *,
    resurrect_dead: bool = False,
    skip_trashed: bool = False,
) -> int:
    """Re-enqueue messages stuck on chunkless zero-vector threads.

    Policy: dead-lettered rows are an operator-visible terminal state
    and are NOT auto-resurrected. The recovery sweep only rescues
    files in queued / no-row state — those represent in-flight or
    cleaned-up work the indexer can safely retry. A row at
    ``status='dead'`` stayed in ``indexing_jobs`` precisely so the
    operator could see that ``max_attempts`` was exhausted; clearing
    that state without operator intent would burn embedder quota
    against the same payload on every container restart and undo
    ``initial_index``'s deliberate ``is_dead`` skip.

    What the sweep handles:

    * **No queue row** (Phase 1 committed but the row was somehow
      cleaned up out-of-band) — re-enqueue with a fresh budget.
    * **Crash mid-batch** (process killed between Phase 1 and
      ``mark_failed``/``mark_succeeded``) — the row stays at
      ``status='queued'`` with the same attempts count as when the
      worker claimed it, and the next ``_drain_queue_batched`` will
      pick it up. ``has_pending_row`` returns True so this branch
      is skipped here, and that's correct — the queue itself owns
      the recovery.
    * **Healthy chunkless subject-fallback threads** (non-zero
      ``threads_vec``) are filtered out at the DB query level.

    What the sweep does NOT handle by default:

    * **Dead-lettered rows** — left alone. To rescue a dead-lettered
      file, an operator confirms the underlying cause is fixed and
      runs ``make requeue-dead`` (``src/requeue_dead.py``), which
      resets dead rows to ``queued`` with a fresh attempt budget.

      Note: simply touching the Maildir file does NOT re-enqueue
      it. The watchdog handles ``on_created`` and ``on_moved``
      events but not ``on_modified``, and ``initial_index``
      consults ``queue.is_dead`` and skips dead-lettered paths
      regardless of file activity.

      ``resurrect_dead=True`` re-enqueues dead rows among this
      sweep's candidates; no production call site uses it.

    Skips files that already have a 'queued' row (active retry
    cascade in flight; clobbering its row would reset the attempts
    counter mid-cascade), and with ``skip_trashed`` (deletion
    reconciliation enabled) T-flagged files, which the reaper owns.

    Returns the number of files re-enqueued for visibility in logs.
    """
    candidates = db.find_zero_vector_chunkless_thread_filepaths()
    if not candidates:
        return 0

    re_enqueued = 0
    skipped_pending = 0
    skipped_dead = 0
    for filepath in candidates:
        if skip_trashed and is_trashed(filepath):
            continue
        if queue.has_pending_row(filepath):
            skipped_pending += 1
            continue
        if not resurrect_dead and queue.is_dead(filepath):
            skipped_dead += 1
            continue
        queue.enqueue(filepath, REASON_RECOVERY)
        re_enqueued += 1

    if re_enqueued or skipped_pending or skipped_dead:
        # Log shape: split the "what happened" facts from the
        # "what's next" implication so the implication only applies
        # to the rows that will actually move. The previous wording
        # ("Their next drain pass should complete the indexing")
        # incorrectly suggested skipped-dead rows would also drain;
        # they will not until an operator intervenes.
        log.warning(
            "recovery sweep: re-enqueued %d zero-vector chunkless "
            "message(s); skipped %d already in active retry; skipped %d "
            "dead-lettered (resurrect_dead=%s).",
            re_enqueued,
            skipped_pending,
            skipped_dead,
            resurrect_dead,
        )
        if re_enqueued:
            log.info(
                "recovery sweep: %d re-enqueued file(s) will be processed on the next drain pass.",
                re_enqueued,
            )
        if skipped_dead and not resurrect_dead:
            log.warning(
                "recovery sweep: %d dead-lettered file(s) remain parked. "
                "They will NOT be drained automatically — see "
                "_recover_zero_vector_threads docstring for the operator "
                "rescue paths.",
                skipped_dead,
            )
    return re_enqueued


def _requeue_stale_extractions(db: Database, queue: IndexingQueue) -> int:
    """Re-queue messages whose cached attachment extraction came from an
    older version of an extractor (see ``extractors.EXTRACTOR_VERSIONS``),
    and, while OCR is on, messages carrying an attachment cached as "OCR
    disabled" (an image, or a PDF without a digital text layer, skipped
    while OCR was off) whose reprocess would run OCR on it (#300).

    Rows are keyed by content hash and extractor module, and each
    occurrence names the row it uses (#928), so only the messages whose
    occurrences use a row are re-queued for it. Reprocessing re-extracts
    a stale row and replaces the attachment's chunks, and the row is
    rewritten with the current version, so each message is re-queued
    once. An "OCR disabled" row is re-queued once OCR is on, and a "no
    extractor" or OLE2 row (an OLE2 payload no extractor read, #694)
    when the occurrence's MIME type or filename now selects another
    module, as when a release starts routing an extension such as
    ``.heic`` (#691) or labels ``.doc`` / ``.xls`` (#935); the re-run
    writes the occurrence's own row, which keeps that once-only too. A
    ``too_large`` row whose payload fits under the current
    ``INDEXER_ATTACHMENT_MAX_BYTES`` (the operator raised the cap) is
    re-extracted, so every message using it is re-queued; the re-run
    rewrites the row, and bytes still over the cap are never re-queued
    (#693).
    Like the zero-vector recovery sweep, files already queued or
    dead-lettered are left alone. Skipped entirely when attachment
    extraction is disabled, since the drain would not re-stamp the rows.
    Returns the number of files re-queued.
    """
    if not INDEXER_ATTACHMENT_EXTRACTION_ENABLED:
        return 0
    stale = [
        name
        for name in db.get_extractor_names()
        if is_stale_extractor(name, ocr_enabled=INDEXER_OCR_ENABLED)
    ]
    filepaths = set(db.find_filepaths_with_extractors(stale))
    # For a "no extractor" or OLE2 row the predicate is only "this
    # occurrence now selects another module"; the OCR setting plays no
    # part in it.
    filepaths.update(
        row["filepath"]
        for row in db.find_no_extractor_attachments()
        if reprocess_reruns_extraction(
            row["extraction_error"], row["extractor_module"], row["content_type"], row["filename"]
        )
    )
    filepaths.update(
        row["filepath"]
        for row in db.find_too_large_attachments()
        if too_large_fits(row["size_bytes"], INDEXER_ATTACHMENT_MAX_BYTES)
    )
    if INDEXER_OCR_ENABLED:
        filepaths.update(
            row["filepath"]
            for row in db.find_ocr_disabled_attachments()
            if reprocess_reruns_extraction(
                row["extraction_error"],
                row["extractor_module"],
                row["content_type"],
                row["filename"],
            )
        )
    re_enqueued = 0
    skipped_dead = 0
    for filepath in sorted(filepaths):
        if queue.has_pending_row(filepath):
            continue
        if queue.is_dead(filepath):
            skipped_dead += 1
            continue
        queue.enqueue(filepath, REASON_REEXTRACT)
        re_enqueued += 1
    if re_enqueued or skipped_dead:
        # A dead-lettered message keeps its stale attachment text (#874).
        log.log(
            logging.WARNING if skipped_dead else logging.INFO,
            "re-queued %d message(s) whose attachments were extracted by an older "
            "extractor version (%s), skipped while OCR was off, had no extractor, "
            "or now fit under INDEXER_ATTACHMENT_MAX_BYTES; skipped %d dead-lettered "
            "(run make requeue-dead to refresh them).",
            re_enqueued,
            ", ".join(sorted(stale)) or "none",
            skipped_dead,
        )
    return re_enqueued


def _enqueue_unindexed_messages(
    db: Database,
    queue: IndexingQueue,
    root: Path,
    reason: str,
    *,
    skip_trashed: bool = False,
    oldest_first: bool = False,
    summary_pass: str | None = None,
) -> int:
    """Walk the Maildir and enqueue every message not yet indexed.

    Shared by the startup scan and the periodic rescan. The watchdog is
    the low-latency path; this walk is the eventual-completeness
    backstop for any file whose event was missed (a restart, event
    coalescing, a delivery while the observer was not running).

    Skips files that are already indexed, dead-lettered, or already
    queued. ``enqueue`` is ``INSERT OR REPLACE``, so re-enqueueing a
    queued row would reset an in-flight retry cascade to zero attempts
    — repeated walks could then retry a failing file forever without
    it ever reaching the dead-letter state. Dead rows are left for the
    operator: the walk only proves the file exists on disk, not that
    anything about it changed since the last failure. (Watchdog
    ``on_created`` / ``on_moved`` events still go through ``enqueue``
    and DO reset prior state, because those signal a real change.)

    ``skip_trashed`` must be True whenever deletion reconciliation is
    enabled. A T-flagged file is then deleted upstream: after the
    reaper removes it from the index (a reaped message's .eml always
    stays on disk) it is unindexed and unqueued, and enqueueing it would
    resurrect the message into search. With reconciliation disabled the
    index is append-only and trashed files are indexed like any other.

    ``oldest_first`` queues each file due at its message time, capped at
    the walk's start (undated and future-dated files: the start), so the
    queue hands the backlog out oldest first across every folder (#699,
    #752). Mail the watcher queues meanwhile is due when it arrives, so
    it follows the backlog. Files already queued and never tried are
    re-dated the same way, so rows an earlier scan or an earlier version
    left (due at their enqueue time) interleave by date with the new
    ones. Otherwise walk order is kept and no headers are read.

    ``summary_pass`` names a maintenance pass: one INFO line with the
    walk's duration and counts is then logged even when it queued
    nothing (#874).

    Returns the number of files enqueued.
    """
    started = _monotonic()
    seen = 0
    skipped_dead = 0
    candidates: list[Path] = []
    already_queued: list[Path] = []
    walk_started = datetime.now(UTC)
    for filepath in _iter_maildir_messages(root):
        seen += 1
        path_str = str(filepath)
        if db.is_indexed(path_str):
            continue
        if skip_trashed and is_trashed(filepath):
            continue
        if queue.is_dead(path_str):
            skipped_dead += 1
            continue
        if queue.has_pending_row(path_str):
            if oldest_first:
                already_queued.append(filepath)
            continue
        candidates.append(filepath)
    due: dict[Path, datetime] = {}
    if oldest_first:
        for p in [*candidates, *already_queued]:
            t = message_sort_time(p)
            due[p] = min(t, walk_started) if t is not None else walk_started
        for p in already_queued:
            queue.redate_untried(str(p), due[p])
        # Equal due times keep this order (rowid), so undated files and
        # same-second messages stay in path order.
        candidates.sort(key=lambda p: (due[p], str(p)))
    for filepath in candidates:
        queue.enqueue(str(filepath), reason, due_at=due.get(filepath))
    enqueued = len(candidates)
    if enqueued or skipped_dead:
        log.info(
            "Maildir walk (%s): enqueued %d message(s), skipped %d dead-lettered.",
            reason,
            enqueued,
            skipped_dead,
        )
    if summary_pass is not None:
        log.info(
            "maintenance pass=%s ms=%d seen=%d queued=%d skipped_dead=%d",
            summary_pass,
            (_monotonic() - started) * 1000,
            seen,
            enqueued,
            skipped_dead,
        )
    return enqueued


def _refresh_folder_watches(
    folder_watches: FolderWatchRefresher,
    db: Database,
    queue: IndexingQueue,
    *,
    skip_trashed: bool = False,
) -> bool:
    """After an mbsync sync attempt, watch the folders it made readable
    (#516).

    mbsync creates folders 0700 and opens them to the indexer only in
    its post-sync permission repair, which the repair marker (after
    every attempt, #524) and the stamp (after a successful one) follow,
    so the watch cannot have been added to a folder created during the
    sync.
    When ``folder_watches`` re-schedules the watch, the old watch is
    closed before the new one walks the tree, and events in that gap
    are lost: heal renames, then queue every unindexed message, as at
    startup. That walk also queues mail already delivered into the
    newly watched folders. If either step raises, the re-schedule stays
    pending and the next call (a marker, stamp or periodic tick) runs
    both again, once per call (#529). Returns whether the recovery ran.
    """
    folder_watches.refresh()
    if not folder_watches.recovery_pending:
        return False
    sweep_paths(db)
    _enqueue_unindexed_messages(db, queue, MAILDIR_PATH, REASON_RESCAN, skip_trashed=skip_trashed)
    folder_watches.recovery_pending = False
    return True


def initial_index(
    db: Database,
    embedder: EmbeddingBackend,
    threader: Threader,
    queue: IndexingQueue,
    *,
    skip_trashed: bool = False,
    breaker: _EmbedOutageBreaker | None = None,
    ingestion_state: _IngestionStateRecorder | None = None,
):
    """Enqueue every unindexed Maildir message and drain the queue.

    ``skip_trashed``: see ``_enqueue_unindexed_messages``. ``breaker``
    is shared with the main loop so an embedder outage that starts
    during the initial drain carries its backoff into steady state
    (the drain returns early while the breaker is open; the main loop
    finishes the queue once the embedder is back). ``ingestion_state``
    acknowledges the sync stamp read before the walk once the walk has
    queued everything that sync delivered.

    Refreshes the health file after every processed message so that
    long initial indexes (large mailboxes, slow embedding service, OCR
    on scanned PDFs) do not exceed ``HEALTH_MAX_AGE_SECONDS`` in the
    healthcheck and cause the container to be reported unhealthy
    mid-scan. Within one message the heartbeat is also refreshed after
    each attachment page read and each embed request, so only a single
    step that stalls past ``HEALTH_MAX_AGE_SECONDS`` trips the
    healthcheck.

    Routing the initial scan through the queue — rather than indexing
    files inline — means a crash or embedding service outage mid-scan leaves the
    untouched work durably queued instead of dropped. The next restart
    resumes from ``indexing_jobs`` rather than rescanning the whole
    Maildir and relying on ``is_indexed`` to filter.
    """
    log.info("Running initial index scan...")
    # Dead-lettered files are not resurrected on routine startup:
    # without that skip, every container restart re-ran the same
    # 5-attempt × 30s backoff cascade against the same poison-pill
    # payloads — observed to add up to ~30 minutes of wasted embedding
    # service load per dead file per restart.
    stamp = ingestion_state.read_stamp() if ingestion_state is not None else None
    _enqueue_unindexed_messages(
        db,
        queue,
        MAILDIR_PATH,
        REASON_INITIAL_SCAN,
        skip_trashed=skip_trashed,
        # Oldest first, so a message is indexed before the replies to it:
        # the threader joins a reply to an indexed parent but never merges
        # a parent into replies indexed before it (#752). Walk order goes
        # folder by folder (Sent before INBOX on the live mailbox), which
        # put about 10,000 of 21,662 replies ahead of every message they
        # reference; oldest first puts none. Reading each header block
        # costs about 12 s for 33,000 messages. Newest first would make
        # recent mail searchable sooner but splits threads until #752's
        # merge exists (owner decision, 2026-10-05).
        oldest_first=True,
    )
    if ingestion_state is not None:
        ingestion_state.acknowledge(stamp)

    # Recovery sweep — re-enqueue messages stuck on chunkless zero-vector
    # threads from a prior crash mid-batch (queued row that mark_failed /
    # mark_succeeded never reached). The standard walk above misses these
    # because ``is_indexed=True``. Dead-lettered rows are intentionally
    # NOT touched here — that policy is uniform across startup and
    # periodic, matching ``initial_index``'s ``is_dead`` skip and the
    # durable queue's bounded-retry contract. See
    # ``_recover_zero_vector_threads`` for the rationale and the
    # ``resurrect_dead=True`` opt-in path. Run BEFORE the drain so
    # recovery rows ride the same batched-index pass as fresh enqueues.
    _recover_zero_vector_threads(db, queue, skip_trashed=skip_trashed)
    # Messages whose attachments an extractor fix would now read
    # differently; re-queued once per version bump.
    _requeue_stale_extractions(db, queue)

    timing_aggregator = TimingAggregator(window=200)
    log.info(
        "Initial index: draining queue with batch_size=%d (cross-message embed batching).",
        INITIAL_INDEX_BATCH_SIZE,
    )
    processed = _drain_queue_batched(
        db,
        embedder,
        threader,
        queue,
        batch_size=INITIAL_INDEX_BATCH_SIZE,
        timing_aggregator=timing_aggregator,
        breaker=breaker,
        skip_trashed=skip_trashed,
        summary_every=TIMING_LOG_EVERY,
    )
    # Always emit a final summary at the end of the initial scan, even
    # if the count was not a multiple of ``TIMING_LOG_EVERY`` — the
    # operator wants to see the cost of the scan they just ran.
    final_line = format_summary(timing_aggregator.summary())
    if final_line:
        log.info(final_line)
    _log_attachment_outcomes(force=True)
    log.info("Initial index complete: %d job(s) processed.", processed)


def _check_embedder_identity(db: Database, embedder) -> None:
    """Record the embedder on a fresh index, else verify it is the one
    that built the index (``src/embed_identity.py``); exit otherwise.

    The calibration vector is also the startup width check: a model whose
    vectors are not ``EMBEDDING_DIM`` wide would otherwise fail on the
    first ``upsert_thread`` with a cryptic sqlite-vec error, so it exits
    here with a clear message, before a fresh index records anything.

    A mismatch exits with the fixed message: the operator restores the
    original embedder or rebuilds the index. A failed calibration request
    has already been retried by ``embed``'s transient-error policy right
    after ``wait_for_ready``, so it exits too, with the scrubbed error,
    and the restart policy tries again.
    """
    try:
        outcome = verify_or_record_embedder(
            db,
            embedder,
            provider=EMBED_MODE,
            endpoint=embedder.base_url,
            model=EMBED_MODEL,
            dimensions=EMBEDDING_DIM,
        )
    except (EmbedderDimensionError, EmbedderIdentityError, CalibrationRequestError) as exc:
        raise SystemExit(str(exc)) from None
    if outcome == "recorded":
        log.info("Recorded embedder identity for this index (model=%s)", EMBED_MODEL)
    else:
        log.info("Embedder identity verified against the index (model=%s)", EMBED_MODEL)


def _load_authority_rules(path: Path) -> AuthorityRules:
    """Load the operator rules file, failing closed on a malformed one.

    The error names the file position, never a pattern (the file holds
    addresses), so it is safe to log.
    """
    try:
        rules = load_authority_rules(path)
    except AuthorityRulesError as exc:
        raise SystemExit(f"Invalid source-authority rules: {exc}") from None
    if rules.pattern_count:
        log.info(
            "Authority rules: %d address and %d domain pattern(s) from %s",
            len(rules.addresses),
            len(rules.domains),
            path,
        )
    else:
        log.info("Authority rules: none at %s; every entity is unclassified", path)
    return rules


def _prune_reaped_records(db: Database) -> None:
    """Expire ``reaped_messages`` records past their retention window."""
    try:
        pruned = db.prune_reaped_messages()
    except Exception as e:
        _streaks[REAPED_RECORD_PRUNE].failed()
        log.error("reaped-record prune failed: %s", type(e).__name__)
        return
    _streaks[REAPED_RECORD_PRUNE].succeeded()
    if pruned:
        log.info("pruned %d expired reaped-message record(s)", pruned)


# A checkpoint blocked this many passes in a row (30 minutes at the
# default interval) logs a WARNING: a reader holding a transaction open
# pins the WAL, which then grows without bound (#875).
WAL_BUSY_WARN_AFTER = 3
_wal_busy_passes = 0
_MIB = 1024 * 1024


def _log_storage(db: Database) -> None:
    """Log the database and WAL file sizes and the free space on their
    volume (#875): two ``stat`` calls and a ``statvfs``."""
    try:
        db_bytes = os.stat(db.path).st_size
        try:
            wal_bytes = os.stat(f"{db.path}-wal").st_size
        except FileNotFoundError:
            wal_bytes = 0
        free_bytes = shutil.disk_usage(Path(db.path).parent).free
    except OSError as e:
        log.warning("storage: size check failed (%s)", type(e).__name__)
        return
    log.info(
        "storage: db=%dMB wal=%dMB free_disk=%dMB",
        db_bytes // _MIB,
        wal_bytes // _MIB,
        free_bytes // _MIB,
    )


def _run_wal_maintenance(db: Database) -> None:
    """One WAL-checkpoint-interval maintenance pass.

    First the FTS5 scrub of each table a reap deleted from since the
    last pass, so a reaped message's index terms leave the live segment
    pages (#641, #670). Then the truncate checkpoint, which copies the
    rewritten pages into ``mail.db`` and clears the WAL frames that
    still held the old ones. A failed scrub leaves its table pending for
    the next pass and does not skip the checkpoint. A checkpoint blocked
    ``WAL_BUSY_WARN_AFTER`` passes in a row warns, and the pass that
    unblocks it logs that; every pass ends with the storage line.
    """
    global _wal_busy_passes
    try:
        started = time.monotonic()
        tables = db.scrub_reaped_fts()
        if tables:
            log.info(
                "fts scrub tables=%s duration=%.2fs",
                ",".join(tables),
                time.monotonic() - started,
            )
    except Exception as e:
        log.error("fts scrub failed: %s", type(e).__name__)
    try:
        busy, log_pages, ckpt_pages = db.wal_checkpoint_truncate()
    except Exception as e:
        _streaks[WAL_CHECKPOINT].failed()
        log.error("wal checkpoint failed: %s", type(e).__name__)
    else:
        _streaks[WAL_CHECKPOINT].succeeded()
        if busy:
            _wal_busy_passes += 1
            if _wal_busy_passes >= WAL_BUSY_WARN_AFTER:
                log.warning(
                    "wal checkpoint blocked %d times in a row; WAL=%d pages",
                    _wal_busy_passes,
                    log_pages,
                )
            else:
                log.debug(
                    "wal_checkpoint busy=%d (a reader pinned WAL frames; next pass will retry)",
                    busy,
                )
        else:
            if _wal_busy_passes >= WAL_BUSY_WARN_AFTER:
                log.info("wal checkpoint unblocked after %d blocked pass(es)", _wal_busy_passes)
            _wal_busy_passes = 0
            if ckpt_pages:
                log.debug("wal_checkpoint truncated %d page(s)", ckpt_pages)
    _log_storage(db)


def _run_watch_refresh(
    folder_watches: FolderWatchRefresher,
    db: Database,
    queue: IndexingQueue,
    *,
    skip_trashed: bool,
    summary: bool = False,
) -> None:
    """``_refresh_folder_watches`` for the main loop: a failure is logged
    by type and retried on the next signal or sweep, and the first
    success after failures logs the recovery (#873). ``summary`` (the
    periodic pass) logs its duration and watch count (#874)."""
    started = _monotonic()
    try:
        _refresh_folder_watches(folder_watches, db, queue, skip_trashed=skip_trashed)
    except Exception as e:
        _streaks[WATCH_REFRESH].failed()
        log.error("Maildir watch refresh failed: %s", type(e).__name__)
        return
    _streaks[WATCH_REFRESH].succeeded()
    if summary:
        log.info(
            "maintenance pass=watch_refresh ms=%d watches=%d",
            (_monotonic() - started) * 1000,
            folder_watches.watched_dirs,
        )


def _run_periodic_reconcile(reconciler: Reconciler | None, db: Database) -> None:
    """One reconciliation interval: sweep and reap when deletion
    reconciliation is on, then expire old reaped-message records (in
    archive mode too, so records from before a switch expire)."""
    if reconciler is not None:
        started = _monotonic()
        try:
            swept = reconciler.sweep()
            reaped = reconciler.reap()
        except Exception as e:
            _streaks[PERIODIC_RECONCILIATION].failed()
            log.error("periodic reconciliation failed: %s", type(e).__name__)
        else:
            _streaks[PERIODIC_RECONCILIATION].succeeded()
            # One line per pass, no-op passes included (#874). The brake
            # is "tripped" when it held back this pass's reaps, "forced"
            # when INDEXER_DELETION_FORCE disables it.
            if reaped.get("aborted"):
                brake = "tripped"
            elif reconciler.config.force:
                brake = "forced"
            else:
                brake = "ok"
            log.info(
                "maintenance pass=reconcile ms=%d tombstoned=%d cleared=%d renamed=%d "
                "missing=%d threads_reaped=%d threads_rebuilt=%d blocked_threads=%d brake=%s",
                (_monotonic() - started) * 1000,
                swept.get("tombstoned", 0),
                swept.get("cleared", 0),
                swept.get("renamed", 0),
                swept.get("missing", 0),
                reaped.get("threads_reaped", 0),
                reaped.get("threads_rebuilt", 0),
                reaped.get("blocked_threads", 0),
                brake,
            )
    _prune_reaped_records(db)


def _run_periodic_rescan(
    db: Database,
    queue: IndexingQueue,
    ingestion_state: _IngestionStateRecorder,
    *,
    skip_trashed: bool,
) -> None:
    """Re-walk the Maildir so a file whose watchdog event was missed is
    still queued, then acknowledge the sync stamp read before the walk."""
    try:
        stamp = ingestion_state.read_stamp()
        _enqueue_unindexed_messages(
            db, queue, MAILDIR_PATH, REASON_RESCAN, skip_trashed=skip_trashed, summary_pass="rescan"
        )
        ingestion_state.acknowledge(stamp)
    except Exception as e:
        _streaks[PERIODIC_RESCAN].failed()
        log.error("periodic Maildir rescan failed: %s", type(e).__name__)
        return
    _streaks[PERIODIC_RESCAN].succeeded()


def _log_reconciler_config(cfg: ReconcilerConfig) -> None:
    if not cfg.enabled:
        log.info(
            "Deletion reconciliation: disabled (archive mode; "
            "unset INDEXER_DELETION_ENABLED to mirror upstream deletions)"
        )
        return
    log.info(
        "Deletion reconciliation: enabled "
        "(mirror mode; grace=%dd, sweep=%ds, max_batch=%.1f%%, force=%s)",
        cfg.grace_days,
        cfg.sweep_interval_secs,
        cfg.max_batch_pct * 100,
        cfg.force,
    )


def main():
    embed_base_url = _validate_embed_config()
    log.info("Starting indexer...")
    log.info("  Maildir: %s", MAILDIR_PATH)
    log.info("  SQLite:  %s", SQLITE_PATH)

    # Before opening the database, so a malformed file fails fast.
    authority_rules = _load_authority_rules(AUTHORITY_RULES_PATH)
    db = Database(SQLITE_PATH)
    reclassified = db.set_authority_rules(authority_rules)
    if reclassified:
        log.info("Authority rules: reclassified %d existing entities", reclassified)
    # Needs only the database: run before the embedder wait and the
    # initial index, which can take hours or never finish (#576).
    _prune_reaped_records(db)
    # Every table starts pending, so this pass scrubs them all once: it
    # covers a reap whose scrub the last run did not reach (#670).
    _run_wal_maintenance(db)
    embedder = OpenAIEmbedder(
        base_url=embed_base_url,
        model=EMBED_MODEL,
        api_key=EMBED_API_KEY,
        batch_size=EMBED_BATCH_SIZE,
        concurrency=EMBED_CONCURRENCY,
    )
    # Log the wire endpoint after construction: ``EMBED_BASE_URL=default``
    # is logged as the official URL it resolves to, not the raw value.
    # ``OpenAIEmbedder.base_url`` reads the URL back from the SDK,
    # matching the mcp-server inference / rerank log lines.
    log.info(
        "  Embedder: %s (model=%s, batch=%d, concurrency=%d)",
        embedder.base_url,
        EMBED_MODEL,
        EMBED_BATCH_SIZE,
        EMBED_CONCURRENCY,
    )
    if EMBED_API_KEY:
        log.info("  Embedder API key: present (Bearer auth enabled)")
    # A loud line when indexing sends mail text off the host (#622).
    _warn_if_remote_endpoint("EMBED_MODE", EMBED_MODE, embedder.base_url, "email text")
    threader = Threader(db)
    touch_health_file()

    queue_cfg = load_queue_config_from_env(os.environ)
    queue = IndexingQueue(
        db,
        max_attempts=queue_cfg["max_attempts"],
        base_backoff_seconds=queue_cfg["base_backoff_seconds"],
    )
    log.info(
        "Indexing queue: max_attempts=%d base_backoff=%ds",
        queue_cfg["max_attempts"],
        queue_cfg["base_backoff_seconds"],
    )
    if INDEXER_MESSAGE_TIMEOUT_SECONDS:
        StallGuard(queue, limit_seconds=INDEXER_MESSAGE_TIMEOUT_SECONDS).start()
        log.info("Stall guard: exits after %ds on one message", INDEXER_MESSAGE_TIMEOUT_SECONDS)
    else:
        log.info("Stall guard: disabled (INDEXER_MESSAGE_TIMEOUT_SECONDS=0)")
    queue_depth = queue.stats()
    if queue_depth["queued"] or queue_depth["dead"]:
        log.info(
            "Queue carry-over from previous run: queued=%d dead=%d",
            queue_depth["queued"],
            queue_depth["dead"],
        )

    reconciler_config = load_config_from_env(os.environ)
    _log_reconciler_config(reconciler_config)
    reconciler: Reconciler | None = None
    if reconciler_config.enabled:
        reconciler = Reconciler(db, embedder, reconciler_config, maildir_root=MAILDIR_PATH)

    # Wait for the embedder to answer, then warm the model.
    embedder.wait_for_ready()

    # Before anything is indexed: one calibration request checks the
    # vector width and never lets vectors from two embedders mix.
    _check_embedder_identity(db, embedder)

    # Start watching BEFORE the initial drain. On a large mailbox the
    # drain runs for hours; mail mbsync delivers in that window would
    # otherwise never be enqueued. Events that land mid-drain are
    # picked up by the same drain-to-empty loop.
    global _ingestion_state
    ingestion_state = _IngestionStateRecorder(db, MAILDIR_PATH)
    _ingestion_state = ingestion_state
    sync_completed = threading.Event()
    directory_created = threading.Event()
    handler = MaildirHandler(
        db,
        queue,
        reconciler=reconciler,
        ingestion_state=ingestion_state,
        sync_completed=sync_completed,
        directory_created=directory_created,
    )
    observer = Observer()
    folder_watches = FolderWatchRefresher(
        MAILDIR_PATH, observer, handler, directory_created=directory_created
    )
    folder_watches.start()
    observer.start()
    # From here every heartbeat checks the thread (#870).
    global _observer
    _observer = observer
    log.info("Watching Maildir for new emails...")

    # Always-on startup rename sweep. mbsync renames files in place for
    # any flag change (e.g. seen ``S`` → seen+replied ``SR``) and when
    # promoting from ``new/`` to ``cur/``. Events that land while the
    # indexer is offline would otherwise leave stale filepaths in
    # ``indexed_files``, which makes later lookups on the renamed file
    # miss. sweep_paths() only updates filepath rows; it does not
    # tombstone missing files, so running it unconditionally preserves
    # archive mode (INDEXER_DELETION_ENABLED=false). It runs before the
    # initial walk so a renamed file's new path is already recorded as
    # indexed: otherwise the walk reprocesses every file renamed while
    # the indexer was down as new mail.
    try:
        sweep_paths(db)
    except Exception as e:
        log.error("startup rename sweep failed: %s", type(e).__name__)

    # Index existing emails
    breaker = _EmbedOutageBreaker()
    initial_index(
        db,
        embedder,
        threader,
        queue,
        skip_trashed=reconciler is not None,
        breaker=breaker,
        ingestion_state=ingestion_state,
    )
    touch_health_file()

    # Startup reconciliation sweep — detect tombstones and path renames that
    # landed while the indexer was offline. Safe to run every startup: it only
    # writes to pending_deletions and updates stored filepaths.
    if reconciler is not None:
        try:
            reconciler.sweep()
            reconciler.reap()
        except Exception as e:
            log.error("startup reconciliation failed: %s", type(e).__name__)
    _prune_reaped_records(db)
    # Scrubs what the startup reap above deleted (#670).
    _run_wal_maintenance(db)

    last_reconcile = time.monotonic()
    last_recovery_sweep = time.monotonic()
    last_wal_checkpoint = time.monotonic()
    timing_aggregator = TimingAggregator(window=200)
    drained_since_log = 0
    last_summary = time.monotonic()
    try:
        while True:
            # Also checks the watcher thread (``_exit_if_watcher_dead``).
            touch_health_file()
            _maybe_log_queue_heartbeat(queue)
            # Drain any queued indexing jobs before yielding to the
            # reconciler so newly-arrived mail is visible in search
            # quickly. Steady state goes through the same batched path
            # initial_index uses, with ``max_passes=1`` so each tick
            # processes at most ``STEADY_STATE_BATCH_SIZE`` messages
            # before yielding to the reconciler / WAL checkpoint /
            # recovery sweep. A burst from an mbsync sync still lands
            # in one bulk embed call instead of N per-message HTTP
            # round-trips.
            try:
                drained = _drain_queue_batched(
                    db,
                    embedder,
                    threader,
                    queue,
                    batch_size=STEADY_STATE_BATCH_SIZE,
                    timing_aggregator=timing_aggregator,
                    max_passes=1,
                    breaker=breaker,
                    skip_trashed=reconciler is not None,
                )
                drained_since_log += drained
                now = time.monotonic()
                if _steady_state_summary_due(
                    drained=drained,
                    drained_since_log=drained_since_log,
                    batch_size=STEADY_STATE_BATCH_SIZE,
                    seconds_since_summary=now - last_summary,
                ):
                    # The timing ring holds recent messages, not ones
                    # since the last line: skip it when none were drained.
                    line = format_summary(timing_aggregator.summary()) if drained_since_log else ""
                    if line:
                        # Tag the periodic timing summary with current
                        # queue depth so operators see when work is
                        # backing up without grepping a separate log.
                        depth = queue.stats()
                        log.info(
                            "%s queued=%d dead=%d",
                            line,
                            depth["queued"],
                            depth["dead"],
                        )
                    _log_attachment_outcomes()
                    drained_since_log = 0
                    last_summary = now
            except Exception as e:
                log.error("queue drain failed: %s", _stage_error(e))

            # A sync attempt's permission repair finished (its marker,
            # or a success stamp): watch any folder it made readable.
            # Cleared first, so a signal during the refresh triggers
            # another.
            if sync_completed.is_set():
                sync_completed.clear()
                _run_watch_refresh(folder_watches, db, queue, skip_trashed=reconciler is not None)

            now = time.monotonic()
            if now - last_reconcile >= reconciler_config.sweep_interval_secs:
                _run_periodic_reconcile(reconciler, db)
                last_reconcile = now

            # Recovery sweep: re-enqueue messages on chunkless
            # zero-vector threads that are STILL retryable. A
            # transient embedder bug that left a row queued-but-stuck
            # heals on its own once the underlying cause clears.
            # Dead-lettered rows are left alone (default policy,
            # uniform with the startup path) — see
            # ``_recover_zero_vector_threads`` for why.
            # The same cadence re-walks the Maildir so a file whose
            # watchdog event was missed still gets indexed eventually.
            if now - last_recovery_sweep >= RECOVERY_SWEEP_INTERVAL_SECS:
                try:
                    _recover_zero_vector_threads(db, queue, skip_trashed=reconciler is not None)
                except Exception as e:
                    log.error("periodic recovery sweep failed: %s", type(e).__name__)
                _run_periodic_rescan(
                    db, queue, ingestion_state, skip_trashed=reconciler is not None
                )
                # Also re-watch here: a lost repair marker, or a failed
                # re-schedule, leaves no watch until the next try.
                _run_watch_refresh(
                    folder_watches,
                    db,
                    queue,
                    skip_trashed=reconciler is not None,
                    summary=True,
                )
                last_recovery_sweep = now

            # WAL checkpoint: keep the WAL file size bounded over a
            # long-running container. SQLite's automatic checkpoint
            # lets the WAL be reused once checkpointed but never
            # shrinks the file, so an explicit periodic
            # ``wal_checkpoint(TRUNCATE)`` is what reclaims space. It
            # can only complete when no reader holds an open read
            # transaction on the WAL. The pass first scrubs the FTS5
            # tables a reap deleted from (#641, #670); see
            # ``_run_wal_maintenance``.
            if now - last_wal_checkpoint >= WAL_CHECKPOINT_INTERVAL_SECS:
                _run_wal_maintenance(db)
                last_wal_checkpoint = now

            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
