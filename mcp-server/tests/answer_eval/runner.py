"""Run ``ask_mailbox`` for one case and capture what its model received.

The registered handler runs unchanged: ``register_intelligence_tools``
registers it on a stub server exactly as ``main.py`` does on FastMCP,
and the case's arguments are passed to it as a client's would be. The
evaluator reimplements neither retrieval nor prompt building.

Two narrow wrappers capture the run, in memory only:

- ``RecordingInference`` wraps the inference client, the boundary every
  prompt crosses, and keeps each request's text and the reply. The
  first request is the prompt after truncation, deduplication, fallback
  thread text and budgeting; a repair call resends it with a fixed
  instruction appended, so the evidence available to the final answer
  is the first request's.
- ``capture_evidence_maps`` wraps ``intelligence._build_evidence`` to
  keep the label -> passage map the handler builds alongside that
  prompt (thread, message, claimant, chunk). ``prompt_consistent``
  then checks every captured label's header is in the prompt the model
  actually received, so the map describes that prompt and not a second
  retrieval.

Nothing here logs mailbox text; the captures stay on the returned
``CaseRun`` and reach disk only through an opted-in detail artifact.
"""

import ast
import asyncio
import email
import email.policy
import email.utils
import functools
import hashlib
import importlib.util
import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

import anthropic
import openai
from fastmcp.exceptions import ToolError
from pydantic import ValidationError
from src.lib.inference import TEMPLATE_RESERVE_TOKENS, InferenceTruncatedError, PromptBudget
from src.lib.security import ProviderResponseError
from src.tools import intelligence
from src.tools.outputs import AskMailboxOutput

from tests.answer_eval.cases import BASELINE_DOMAIN, Case

# Statuses a run can end in. Only ``ok`` has an output to grade.
RUN_STATUSES = ("ok", "tool_error", "timeout", "invalid_output", "runner_error", "skipped")


class NonSyntheticIndexError(RuntimeError):
    """The index is not the synthetic baseline corpus."""


class ProviderBillingError(RuntimeError):
    """A provider refused a call for billing, credit or a subscription
    usage limit (#839): every later call would fail the same way, so the
    run stops. Fixed text only."""

    def __init__(self, layer: str) -> None:
        super().__init__(
            f"the {layer} provider refused a call for billing, credit or a usage limit; "
            "the run stopped and no report was written"
        )


def is_billing_error(error: BaseException) -> bool:
    """True for a provider's billing or credit refusal, matched by SDK
    status-error type, HTTP status and the error body's ``type`` field;
    never by the provider's message text.

    - Anthropic: ``APIStatusError`` with status 402 and type
      ``billing_error``. Anthropic has also answered an exhausted credit
      balance with 400 ``invalid_request_error``, which only the message
      tells apart from any other bad request, so that is not matched.
    - OpenAI-compatible: ``APIStatusError`` with status 429 and type
      ``insufficient_quota`` (a rate limit is 429 with another type).
    """
    if isinstance(error, anthropic.APIStatusError):
        return error.status_code == 402 and error.type == "billing_error"
    if isinstance(error, openai.APIStatusError):
        return error.status_code == 429 and error.type == "insufficient_quota"
    return False


@dataclass(frozen=True)
class Passage:
    """One labelled passage of the prompt sent to inference."""

    label: str
    thread_id: str
    message_id: str | None
    claimant_id: str | None
    chunk_id: str | None
    source: str  # "body", "attachment" or "thread" (the thread's indexed text)
    text: str = field(repr=False)
    truncated: bool = False  # cut to fit the prompt budget
    # The header ``ask_mailbox`` rendered above the passage
    # (``intelligence._piece_header``): label, message, sender, sent date
    # and, for an attachment chunk, its name. Sender-controlled.
    header: str = field(default="", repr=False)


@dataclass
class InferenceCall:
    system: str = field(repr=False)
    user: str = field(repr=False)
    ms: float = 0.0
    outcome: str = "ok"  # "ok", "truncated" or "error"
    response: str | None = field(default=None, repr=False)


@dataclass
class CaseRun:
    case_id: str
    status: str
    error: str | None = None  # fixed category or exception type name
    output: AskMailboxOutput | None = None
    passages: dict[str, Passage] = field(default_factory=dict)
    calls: list[InferenceCall] = field(default_factory=list)
    prompt_consistent: bool = True
    timings_ms: dict[str, float] = field(default_factory=dict)
    error_detail: str | None = field(default=None, repr=False)  # detail artifact only
    billing_error: bool = False  # a call hit ``is_billing_error``: stop the run


class RecordingInference:
    """Inference client wrapper that records every request and reply.

    It sees the provider's exception before ``ask_mailbox`` turns it into
    a ``ToolError``, so it notes a billing refusal there (#839)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.mode = inner.mode
        self.base_url = getattr(inner, "base_url", "")
        self.calls: list[InferenceCall] = []
        self.billing_error = False

    async def complete(self, system: str, user: str) -> str:
        call = InferenceCall(system=system, user=user)
        self.calls.append(call)
        start = time.perf_counter()
        try:
            call.response = await self._inner.complete(system, user)
            return call.response
        except InferenceTruncatedError as e:
            call.outcome, call.response = "truncated", e.partial
            raise
        except BaseException as e:
            call.outcome = "error"
            self.billing_error = self.billing_error or is_billing_error(e)
            raise
        finally:
            call.ms = (time.perf_counter() - start) * 1000


class TimedEmbedder:
    """Embed client wrapper that adds up the time spent embedding."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.ms = 0.0

    async def embed(self, text: str) -> list[float]:
        start = time.perf_counter()
        try:
            return await self._inner.embed(text)
        finally:
            self.ms += (time.perf_counter() - start) * 1000


class PrecomputedEmbedder:
    """Query vectors written by the baseline build (``query_vectors.json``).

    The baseline index uses a hashed embedder that only the indexer
    environment has, so the build embeds every case question up front
    and this returns those vectors: the same embedder for query and
    index, as retrieval requires.
    """

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    async def embed(self, text: str) -> list[float]:
        try:
            return self._vectors[text]
        except KeyError:
            raise ProviderResponseError(
                "No precomputed query vector for this question; rebuild the index with "
                "the case file (tests.baseline.build <out> <golden.json> <cases.json>)"
            ) from None


class _ToolServer:
    """The FastMCP surface ``register_intelligence_tools`` uses."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *_args: Any, **_kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@contextmanager
def capture_evidence_maps(sink: list[dict[str, Any]]) -> Iterator[None]:
    """Record the ``evidence_map`` of every ``_build_evidence`` call.

    The handler looks the function up as a module global on each call,
    so replacing the attribute reaches it; the original runs unchanged.
    Runs are sequential, so the swap is not shared between cases.
    """
    original = intelligence._build_evidence

    def spy(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        evidence_map = kwargs.get("evidence_map")
        if evidence_map is not None:
            sink.append(evidence_map)
        return result

    intelligence._build_evidence = spy  # type: ignore[assignment]
    try:
        yield
    finally:
        intelligence._build_evidence = original


def _passage(ref: Any) -> Passage:
    chunk = ref.chunk
    # The scope label the header showed, if any (#755).
    scope = getattr(ref, "header_scope", None)
    header = intelligence._piece_header(chunk, ref.char_end or 0, ref.label, scope)
    if chunk is None:
        return Passage(
            ref.label, ref.thread_id, None, None, None, "thread", ref.text, header=header
        )
    source = "attachment" if chunk.attachment_id is not None else "body"
    # ``char_end`` is short of the chunk's end when the passage was cut.
    truncated = ref.char_end is not None and ref.char_end < chunk.char_end
    return Passage(
        ref.label,
        ref.thread_id,
        chunk.message_id,
        chunk.claimant_id,
        chunk.chunk_id,
        source,
        ref.text,
        truncated,
        header,
    )


def prompt_budget_for(case: Case, default: PromptBudget) -> PromptBudget:
    """The case's prompt allowance, or the configured one.

    A case's ``prompt_tokens`` fixes the prompt allowance itself, so it
    tests the same omission whatever reply reserve is configured.
    """
    if case.prompt_tokens is None:
        return default
    reserve = default.max_output_tokens
    return PromptBudget(
        context_tokens=case.prompt_tokens + reserve + TEMPLATE_RESERVE_TOKENS,
        max_output_tokens=reserve,
    )


@dataclass
class RunContext:
    db: Any
    embed_client: Any
    inference_client: Any
    prompt_budget: PromptBudget
    expected_embed_dim: int | None = None
    secret_values: list[str] = field(default_factory=list, repr=False)
    case_timeout_secs: float = 900.0


async def run_case(case: Case, ctx: RunContext) -> CaseRun:
    """Call the real ``ask_mailbox`` handler for ``case`` and capture it."""
    recorder = RecordingInference(ctx.inference_client)
    embedder = TimedEmbedder(ctx.embed_client)
    server = _ToolServer()
    intelligence.register_intelligence_tools(
        server,
        ctx.db,
        embedder,
        recorder,
        reranker=None,
        secret_values=ctx.secret_values,
        expected_embed_dim=ctx.expected_embed_dim,
        prompt_budget=prompt_budget_for(case, ctx.prompt_budget),
    )
    maps: list[dict[str, Any]] = []
    run = CaseRun(case_id=case.id, status="ok")
    start = time.perf_counter()
    with capture_evidence_maps(maps):
        try:
            result = await asyncio.wait_for(
                server.tools["ask_mailbox"](**case.arguments), ctx.case_timeout_secs
            )
        except TimeoutError:
            run.status, run.error = "timeout", "timeout"
        except ToolError as e:
            # The tool's own message is already sanitized, but it can quote
            # a rejected argument; only the detail artifact keeps it.
            run.status, run.error, run.error_detail = "tool_error", "tool_error", str(e)
        except Exception as e:
            run.status, run.error = "runner_error", type(e).__name__
    total_ms = (time.perf_counter() - start) * 1000

    run.calls = recorder.calls
    run.billing_error = recorder.billing_error
    if maps:
        run.passages = {label: _passage(ref) for label, ref in maps[-1].items()}
    if run.calls:
        first = run.calls[0].user
        run.prompt_consistent = all(f"[{label} |" in first for label in run.passages)
    inference_ms = sum(c.ms for c in run.calls)
    run.timings_ms = {
        "answer_total": round(total_ms, 1),
        "query_embedding": round(embedder.ms, 1),
        "inference": round(inference_ms, 1),
        "retrieval_and_prompt": round(max(total_ms - embedder.ms - inference_ms, 0.0), 1),
    }
    if run.status == "ok":
        try:
            run.output = AskMailboxOutput.model_validate(result.structured_content)
        except ValidationError:
            run.status, run.error = "invalid_output", "invalid_output"
    return run


CORPUS_PATH = Path(__file__).resolve().parents[3] / "indexer" / "tests" / "baseline" / "corpus.py"
PARSER_PATH = CORPUS_PATH.parents[2] / "src" / "parser.py"
_TOKEN = re.compile(r"[^\W_]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.casefold()))


# The indexer renders a thread's ``body_text`` under a header template
# (``Subject:``, ``From:``, ``Date:`` with ISO timestamps,
# ``Participants:``) and cuts it at a token budget, which can split the
# last word. Thread rows may use those words, ISO date fragments and
# prefixes of corpus words; chunk text must use corpus words only.
_THREAD_TEMPLATE_WORDS = frozenset({"subject", "from", "to", "cc", "date", "participants"})
_ISO_FRAGMENT = re.compile(r"[0-9]+(?:t[0-9]+)?")


def _thread_word_ok(word: str, allowed: set[str]) -> bool:
    return (
        word in _THREAD_TEMPLATE_WORDS
        or _ISO_FRAGMENT.fullmatch(word) is not None
        or any(token.startswith(word) for token in allowed)
    )


@dataclass(frozen=True)
class CorpusMessage:
    sha256: str  # of the message's bytes, in full
    thread_id: str
    sent_at: str  # ``messages.sent_at`` as the indexer stores it
    tokens: set[str] = field(repr=False)


@functools.cache
def claimant_hash_chars(path: Path = PARSER_PATH) -> int:
    """The indexer's ``parser.CLAIMANT_HASH_CHARS`` (16 today), the exact
    length of a claimant ID's hash suffix.

    It lives in the other service, whose parser imports dependencies
    this one lacks, so the integer is read from the committed source's
    module-level assignment with ``ast``, without importing or running
    it, as ``corpus_manifest`` loads the corpus by path.
    """
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["CLAIMANT_HASH_CHARS"]
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int
        ):
            return node.value.value
    raise NonSyntheticIndexError("the indexer's claimant hash length could not be read")


def corpus_manifest(path: Path = CORPUS_PATH) -> dict[str, CorpusMessage]:
    """Message-ID -> its bytes' SHA-256, thread and word tokens, for every
    committed corpus message.

    Built from the committed ``corpus.py`` (stdlib only, loaded by path:
    both services own a top-level ``tests`` package), which serializes
    byte-identically; the indexer's claimant ID is the Message-ID plus a
    prefix of that SHA-256. The tokens cover every part's headers and
    every decoded text part and binary attachment, our own trusted bytes. ``sent_at`` is the
    ``Date:`` header as the indexer normalizes it (``parser._parse_date``:
    UTC, ISO format); the corpus writes no ``Received:`` header, so the
    indexer stores no ``occurred_at``.
    """
    spec = importlib.util.spec_from_file_location("answer_eval_baseline_corpus", path)
    if spec is None or spec.loader is None:
        raise NonSyntheticIndexError("the committed synthetic corpus could not be loaded")
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    manifest: dict[str, CorpusMessage] = {}
    for n in sorted(corpus.THREADS):
        for index, msg in enumerate(corpus.THREADS[n]):
            raw = corpus.build_message(n, index, msg)
            message_id = f"t{n:02d}.{index + 1}{BASELINE_DOMAIN}"
            parsed = email.message_from_bytes(raw, policy=email.policy.default)
            sent = email.utils.parsedate_to_datetime(str(parsed["Date"]))
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=UTC)
            text = [message_id]
            for part in parsed.walk():  # every part's headers: attachment names and types
                text += [str(v) for v in part.values()]
                if part.get_content_maintype() == "text":
                    text.append(part.get_content())
                elif not part.is_multipart():
                    # A binary attachment's own bytes: the corpus's PDFs
                    # (``_minimal_pdf``) carry their text uncompressed,
                    # so the words an extractor finds are among these.
                    payload = part.get_payload(decode=True)
                    text.append(payload.decode("utf-8", "replace"))
            manifest[message_id] = CorpusMessage(
                hashlib.sha256(raw).hexdigest(),
                f"t{n:02d}.1{BASELINE_DOMAIN}",
                sent.astimezone(UTC).isoformat(),
                _tokens(" ".join(text)),
            )
    return manifest


def _claimant_message(claimant: str, manifest: dict[str, CorpusMessage]) -> str | None:
    """The corpus Message-ID ``claimant`` belongs to, or None when it is
    not ``<corpus Message-ID>#<first claimant_hash_chars() hex digits of
    that message's SHA-256>``, as the indexer derives it."""
    message_id, _, suffix = claimant.rpartition("#")
    entry = manifest.get(message_id)
    if entry is None or suffix != entry.sha256[: claimant_hash_chars()]:
        return None
    return message_id


def index_identity(db: Any, manifest_path: Path = CORPUS_PATH) -> dict[str, object]:
    """Fingerprint of the index, refusing anything but the committed corpus.

    The evaluation sends evidence to the configured providers, and a
    real mailbox is out of its scope, so the index must hold exactly one
    claimant per committed corpus message, each its Message-ID plus the
    first ``claimant_hash_chars()`` hex digits of that message's SHA-256
    (``corpus_manifest``); each message's stored Message-ID (a passage's
    origin in the judge prompt) and dates (the labelled chunk header's)
    must be the corpus message's; and every indexed text a prompt can
    carry may use only words of the corpus messages it belongs to: chunk
    text, message subjects, participants (the chunk header's sender),
    attachment names and types, and thread subjects, display subjects,
    snippets, bodies and participants. Private text stored under copied
    baseline IDs fails the last two checks. Messages name no content.
    """
    manifest = corpus_manifest(manifest_path)
    refused = NonSyntheticIndexError(
        "the index is not the committed synthetic baseline corpus; the answer evaluation "
        "runs only against an index built by tests.baseline.build"
    )
    with closing(db._connect()) as conn:
        claimants = {str(c) for (c,) in conn.execute("SELECT claimant_id FROM messages")}
        owner = {c: _claimant_message(c, manifest) for c in claimants}
        owned = [m for m in owner.values() if m is not None]
        # Every claimant a corpus message, every corpus message claimed once.
        if len(owned) != len(owner) or sorted(owned) != sorted(manifest):
            raise refused
        # The claimant's own Message-ID, and its dates as the indexer
        # stores them: no Received header, so no occurred_at.
        for claimant, stored_id, sent_at, occurred_at in conn.execute(
            "SELECT claimant_id, message_id, sent_at, occurred_at FROM messages"
        ):
            message_id = owner[str(claimant)]
            if message_id is None or (stored_id, sent_at, occurred_at) != (
                message_id,
                manifest[message_id].sent_at,
                None,
            ):
                raise refused
        # Per-message text a prompt can carry: chunk text, and the chunk
        # header's sender and attachment name and type.
        per_message = (
            "SELECT claimant_id, text FROM message_chunks",
            "SELECT claimant_id, subject FROM messages",
            "SELECT claimant_id, coalesce(name, '') || ' ' || coalesce(address, '') "
            "FROM message_participants",
            "SELECT claimant_id, coalesce(filename, '') || ' ' || coalesce(content_type, '') "
            "FROM attachments",
        )
        for query in per_message:
            for claimant, text in conn.execute(query):
                message_id = owner.get(str(claimant))
                if message_id is None:
                    raise refused
                if not _tokens(str(text or "")) <= manifest[message_id].tokens:
                    raise refused
        thread_tokens: dict[str, set[str]] = {}
        for entry in manifest.values():
            thread_tokens.setdefault(entry.thread_id, set()).update(entry.tokens)
        rows = conn.execute(
            "SELECT thread_id, subject, display_subject, snippet, body_text, participants "
            "FROM threads"
        )
        for thread_id, subject, display_subject, snippet, body_text, participants in rows:
            allowed = thread_tokens.get(str(thread_id), set())
            try:  # a JSON list, whose \u escapes would split names into odd tokens
                names = " ".join(str(p) for p in json.loads(participants or "[]"))
            except ValueError, TypeError:
                names = str(participants)
            texts = (subject, display_subject, snippet, body_text, names)
            words = _tokens(" ".join(str(t or "") for t in texts))
            if not all(_thread_word_ok(w, allowed) for w in words - allowed):
                raise refused
    digest = hashlib.sha256("\n".join(sorted(claimants)).encode()).hexdigest()
    return {"corpus": "synthetic-baseline", "messages": len(claimants), "index_sha256": digest}
