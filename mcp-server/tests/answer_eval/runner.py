"""Run a case's tool and capture what its model received.

The registered handler (``ask_mailbox`` or ``summarize_thread``, #656;
the experimental ``brief_issue`` or ``check_conclusion``, #1240) runs
unchanged: ``register_intelligence_tools`` and
``register_experimental_tools`` register them on a stub server exactly
as ``main.py`` does on FastMCP, and the case's arguments are passed to
it as a client's would be. The stub always has the experimental tools:
the evaluation enables them for itself, whatever the server's
``MCP_EXPERIMENTAL_TOOLS`` says, and reads no such setting. The
evaluator reimplements neither retrieval nor prompt building.

Two narrow wrappers capture the run, in memory only:

- ``RecordingInference`` wraps the inference client, the boundary every
  prompt crosses, and keeps each request's text and the reply. The
  first request is the prompt after truncation, deduplication, fallback
  thread text and budgeting; a repair call resends it with a fixed
  instruction appended, so the evidence available to the final answer
  is the first request's.
- ``capture_evidence_maps`` wraps the handlers' evidence builders
  (``intelligence._build_evidence``, also under ``brief``'s own import of
  it, and ``_summarize_context``) to keep
  the label -> passage maps built alongside those prompts (thread,
  message, claimant, chunk); ``adapters.select_passages`` picks the map
  that describes the prompt sent. ``prompt_consistent`` then checks
  every captured label's header is in the prompt the model actually
  received, so the map describes that prompt and not a second retrieval.

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
from dataclasses import dataclass, field, replace
from datetime import UTC
from pathlib import Path
from typing import Any

import anthropic
import openai
from fastmcp.exceptions import ToolError
from pydantic import ValidationError
from src.lib.inference import TEMPLATE_RESERVE_TOKENS, InferenceTruncatedError, PromptBudget
from src.lib.security import ProviderResponseError
from src.tools import brief, intelligence
from src.tools.outputs import (
    AskMailboxOutput,
    BriefIssueOutput,
    CheckConclusionOutput,
    SummarizeThreadOutput,
)

from tests.answer_eval.adapters import (
    OUTPUT_MODELS,
    AnswerView,
    select_passages,
    view_of,
    window_cut_labels,
)
from tests.answer_eval.cases import Case

ToolOutput = AskMailboxOutput | SummarizeThreadOutput | BriefIssueOutput | CheckConclusionOutput

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
    # The structured-output schema the tool passed, if any (#1095): the
    # caller's schema shape, not mail content.
    json_schema: dict | None = field(default=None, repr=False)


@dataclass
class CaseRun:
    case_id: str
    status: str
    error: str | None = None  # fixed category or exception type name
    output: ToolOutput | None = None
    passages: dict[str, Passage] = field(default_factory=dict)
    calls: list[InferenceCall] = field(default_factory=list)
    prompt_consistent: bool = True
    timings_ms: dict[str, float] = field(default_factory=dict)
    error_detail: str | None = field(default=None, repr=False)  # detail artifact only
    billing_error: bool = False  # a call hit ``is_billing_error``: stop the run
    tool: str = "ask_mailbox"  # the case's tool, which ``view`` reads ``output`` through

    @property
    def view(self) -> AnswerView:
        """The output as the graders, judge and reports read it
        (``adapters.view_of``); only a run with an output has one."""
        if self.output is None:
            raise ValueError("a run without output has no view")
        return view_of(self.tool, self.output)


class RecordingInference:
    """Inference client wrapper that records every request and reply.

    It sees the provider's exception before ``ask_mailbox`` turns it into
    a ``ToolError``, so it notes a billing refusal there (#839)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.mode = inner.mode
        self.base_url = getattr(inner, "base_url", "")
        # ``extract_from_emails`` reads this before sending a schema (#808).
        self.structured_output = getattr(inner, "structured_output", False)
        self.calls: list[InferenceCall] = []
        self.billing_error = False

    async def complete(self, system: str, user: str, json_schema: dict | None = None) -> str:
        call = InferenceCall(system=system, user=user, json_schema=json_schema)
        self.calls.append(call)
        start = time.perf_counter()
        try:
            # As ``intelligence.llm_complete`` does: the keyword is passed
            # only when set, so a client taking ``(system, user)`` works.
            if json_schema is None:
                call.response = await self._inner.complete(system, user)
            else:
                call.response = await self._inner.complete(system, user, json_schema=json_schema)
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


# The handlers' evidence builders, each taking an ``evidence_map`` keyword,
# as the module global each handler looks up: ``ask_mailbox`` builds
# through ``intelligence._build_evidence``, ``summarize_thread`` through
# ``_summarize_context``, and the experimental tools through ``brief``'s
# own imported name for ``_build_evidence`` (#1240).
_EVIDENCE_BUILDERS = (
    (intelligence, "_build_evidence"),
    (intelligence, "_summarize_context"),
    (brief, "_build_evidence"),
)


@contextmanager
def capture_evidence_maps(sink: list[dict[str, Any]]) -> Iterator[None]:
    """Record the ``evidence_map`` of every evidence-builder call, in
    call order.

    The handlers look the functions up as module globals on each call,
    so replacing the attributes reaches them; the originals run
    unchanged. Runs are sequential, so the swap is not shared between
    cases.
    """
    originals = [(module, name, getattr(module, name)) for module, name in _EVIDENCE_BUILDERS]

    def spy_for(original: Callable[..., Any]) -> Callable[..., Any]:
        def spy(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            evidence_map = kwargs.get("evidence_map")
            if evidence_map is not None:
                sink.append(evidence_map)
            return result

        return spy

    for module, name, original in originals:
        setattr(module, name, spy_for(original))
    try:
        yield
    finally:
        for module, name, original in originals:
            setattr(module, name, original)


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
    """Call the real handler of the case's tool for ``case`` and capture it."""
    recorder = RecordingInference(ctx.inference_client)
    embedder = TimedEmbedder(ctx.embed_client)
    server = _ToolServer()
    # The experimental tools too, for this stub only (#1240).
    for register in (intelligence.register_intelligence_tools, brief.register_experimental_tools):
        register(
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
    run = CaseRun(case_id=case.id, status="ok", tool=case.tool)
    start = time.perf_counter()
    with capture_evidence_maps(maps):
        try:
            result = await asyncio.wait_for(
                server.tools[case.tool](**case.arguments), ctx.case_timeout_secs
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
    shown = select_passages(case.tool, maps)
    run.passages = {label: _passage(ref) for label, ref in shown.items()}
    # A summary passage the window cut short, E1's thread text included,
    # which has no chunk offsets for ``_passage`` to compare.
    for label in window_cut_labels(case.tool, maps):
        run.passages[label] = replace(run.passages[label], truncated=True)
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
            run.output = OUTPUT_MODELS[case.tool].model_validate(result.structured_content)
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
    message_id: str  # as the indexer's parser derives it from the built file
    sha256: str  # of the message's bytes, in full
    thread_id: str  # the thread root's ``message_id``
    sent_at: str  # ``messages.sent_at`` as the indexer stores it
    tokens: set[str] = field(repr=False)


def _parsed_message_id(raw: bytes) -> str:
    """The Message-ID as the indexer's parser derives it from a file's
    bytes (``parser.parse_message``: a compat32 parse, then
    ``_clean_id``)."""
    header = str(email.message_from_bytes(raw).get("Message-ID", ""))
    message_id = header.strip().strip("<>").strip()
    if not message_id:
        raise NonSyntheticIndexError("a committed synthetic corpus message has no Message-ID")
    return message_id


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
    """Claimant ID -> its Message-ID, bytes' SHA-256, thread, date and
    word tokens, for every committed corpus message.

    Built from the committed ``corpus.py`` (stdlib only, loaded by path:
    both services own a top-level ``tests`` package), which serializes
    byte-identically. Each built file's claimant ID is the indexer's
    (``parser.claimant_id``): the Message-ID the parser derives from the
    file plus the first ``claimant_hash_chars()`` hex digits of the
    file's SHA-256, so two files sharing a Message-ID with different
    bytes are two entries (#1275). A claimant built twice (a
    byte-identical copy) is an error. A thread's ID is its root file's
    Message-ID. The tokens cover every part's headers and every decoded
    text part and binary attachment, our own trusted bytes, and the text
    the corpus says its images show (``OCR_TEXT``). ``sent_at`` is the
    ``Date:`` header as the indexer normalizes it (``parser._parse_date``:
    UTC, ISO format); the corpus writes no ``Received:`` header, so the
    indexer stores no ``occurred_at``.
    """
    spec = importlib.util.spec_from_file_location("answer_eval_baseline_corpus", path)
    if spec is None or spec.loader is None:
        raise NonSyntheticIndexError("the committed synthetic corpus could not be loaded")
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    hash_chars = claimant_hash_chars()
    manifest: dict[str, CorpusMessage] = {}
    for n in sorted(corpus.THREADS):
        built = [corpus.build_message(n, i, msg) for i, msg in enumerate(corpus.THREADS[n])]
        thread_id = _parsed_message_id(built[0])
        for raw in built:
            message_id = _parsed_message_id(raw)
            sha256 = hashlib.sha256(raw).hexdigest()
            claimant = f"{message_id}#{sha256[:hash_chars]}"
            if claimant in manifest:
                raise NonSyntheticIndexError(
                    "the committed synthetic corpus builds one claimant ID more than once"
                )
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
                    # (``_minimal_pdf``) and its DOCX and XLSX files (stored,
                    # not deflated, by ``_ooxml``) carry their text
                    # uncompressed, so the words an extractor finds are
                    # among these.
                    payload = part.get_payload(decode=True)
                    assert isinstance(payload, bytes)  # a non-multipart part decodes to bytes
                    text.append(payload.decode("utf-8", "replace"))
                    # An image's words are what OCR reads from it: the
                    # corpus states the text its committed images show
                    # (#908), keyed by attachment filename.
                    text.append(corpus.OCR_TEXT.get(part.get_filename() or "", ""))
            manifest[claimant] = CorpusMessage(
                message_id,
                sha256,
                thread_id,
                sent.astimezone(UTC).isoformat(),
                _tokens(" ".join(text)),
            )
    return manifest


def index_identity(db: Any, manifest_path: Path = CORPUS_PATH) -> dict[str, object]:
    """Fingerprint of the index, refusing anything but the committed corpus.

    The evaluation sends evidence to the configured providers, and a
    real mailbox is out of its scope, so the index's claimant IDs must
    be exactly the set ``corpus_manifest`` builds (each built file's
    Message-ID plus the first ``claimant_hash_chars()`` hex digits of
    its SHA-256; two files sharing a Message-ID are two claimants,
    #1275). Each claimant is then checked against its own entry, never
    a sibling's: its stored Message-ID (a passage's origin in the judge
    prompt) and dates (the labelled chunk header's) must be the entry's,
    and every per-message text a prompt can carry (chunk text, message
    subject, participants (the chunk header's sender), attachment names
    and types) may use only that file's words. Thread subjects, display
    subjects, snippets, bodies and participants may use the words of
    the thread's files together. This is a token allowlist, not byte
    authentication: private text stored under copied baseline IDs is
    refused when it uses a word its file lacks. Messages name no
    content.
    """
    manifest = corpus_manifest(manifest_path)
    refused = NonSyntheticIndexError(
        "the index is not the committed synthetic baseline corpus; the answer evaluation "
        "runs only against an index built by tests.baseline.build"
    )
    with closing(db._connect()) as conn:
        claimants = [str(c) for (c,) in conn.execute("SELECT claimant_id FROM messages")]
        # Every claimant a built corpus file, every built file claimed once.
        if sorted(claimants) != sorted(manifest):
            raise refused
        # The claimant's own Message-ID, and its dates as the indexer
        # stores them: no Received header, so no occurred_at.
        for claimant, stored_id, sent_at, occurred_at in conn.execute(
            "SELECT claimant_id, message_id, sent_at, occurred_at FROM messages"
        ):
            entry = manifest.get(str(claimant))
            if entry is None or (stored_id, sent_at, occurred_at) != (
                entry.message_id,
                entry.sent_at,
                None,
            ):
                raise refused
        # Per-message text a prompt can carry: chunk text, and the chunk
        # header's sender and attachment name and type, each against its
        # own claimant's tokens.
        per_message = (
            "SELECT claimant_id, text FROM message_chunks",
            "SELECT claimant_id, subject FROM messages",
            "SELECT claimant_id, coalesce(name, '') || ' ' || coalesce(address, '') "
            "FROM message_participants",
            # Every stored display name (#1140), which find_contact reports.
            "SELECT claimant_id, name FROM message_participant_names",
            "SELECT claimant_id, coalesce(filename, '') || ' ' || coalesce(content_type, '') "
            "FROM attachments",
        )
        for query in per_message:
            for claimant, text in conn.execute(query):
                entry = manifest.get(str(claimant))
                if entry is None or not _tokens(str(text or "")) <= entry.tokens:
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
