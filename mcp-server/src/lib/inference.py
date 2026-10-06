"""Inference client for the MCP server.

``INFERENCE_MODE`` selects the protocol/SDK:

- ``anthropic``: Anthropic-compatible Messages API via the
  official ``anthropic`` SDK. Unlocks prompt caching, typed tool use,
  streaming, and future API surface (extended thinking, batch, token
  counting) without hand-rolling the wire format.
- ``openai``: OpenAI-compatible chat completions via the official
  ``openai`` SDK. Operator-supplied compat servers (LM Studio, vLLM,
  ``mlx_lm.server``, DeepInfra, OpenRouter, etc.) target the OpenAI
  SDK as their reference client by design, so pointing the SDK at
  them via ``base_url`` is the supported path.
- ``none`` (the default, #750): layer disabled, so nothing is sent to
  an inference provider until one is chosen. ``main.py`` does not instantiate
  ``InferenceClient`` and the intelligence tool group is not
  registered.

Required-vars validation lives in ``main.py``. There is no fallback
between modes: a misconfigured ``anthropic`` install fails at
startup rather than silently routing to OpenAI.

Each backend imports its SDK lazily inside ``__init__`` so a deployment
that picks one mode never imports the other's SDK — no cold-start cost,
no transitive dependency surface, and no exposure to a future
import-time issue in an SDK the operator isn't using.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from .security import (
    ProviderResponseError,
    safe_provider_exception_text,
    same_origin_request_hook,
)

log = logging.getLogger("mcp.inference")

# Per-operation HTTP timeout for one completion: each connect, read or
# write must make progress within it. It is not a total-call deadline —
# a provider that streams a response in small fragments can keep one
# call alive past it (a total deadline is tracked by #287). Qwen3 in
# thinking mode can run ~1-2 minutes for a long answer; Anthropic
# Messages calls usually return faster but reasoning-heavy prompts can
# stretch. 300 s catches truly stuck calls without false-positiving on
# slow-but-progressing inference. Operators on slow networks can
# override via ``INFERENCE_TIMEOUT_SECS``; resolution happens in
# ``main.py`` so the library code stays env-free for tests.
DEFAULT_COMPLETE_TIMEOUT_SECS = 300.0

# Default ``max_tokens``. The Anthropic Messages API requires the
# field; the OpenAI Chat Completions API accepts it too (most
# OpenAI-compatible servers — vLLM, mlx_lm.server, LM Studio,
# DeepInfra — honor it). 1024 fits brief summaries and per-thread
# extraction; raise for detailed summaries on long threads. Operator
# overrides via ``INFERENCE_MAX_TOKENS``.
DEFAULT_MAX_TOKENS = 1024

# Default model context window in tokens: the prompt and the reply
# together must fit in it (#285). 32,768 is the native window of the
# smaller current open models; hosted models have more. At this default
# the per-tool character caps, not the window, bound every prompt, so
# prompts are what they were before the window was counted. An operator
# running a small local model sets ``INFERENCE_CONTEXT_TOKENS`` to its
# window (the small-model profile), and evidence is cut to fit.
DEFAULT_CONTEXT_TOKENS = 32768

# Anthropic-mode defaults (#764). Current Claude models think before
# they answer and count the thinking against ``max_tokens``, so 1024
# cuts answers short. The window grows with the reply so the prompt room
# (window less reply) stays at least what the defaults above leave and
# the per-tool character caps still bind first. Hosted Claude windows
# are far larger. openai mode keeps the defaults above: a 32k local
# model must still fit the whole request.
ANTHROPIC_DEFAULT_MAX_TOKENS = 16000
ANTHROPIC_DEFAULT_CONTEXT_TOKENS = 48000


def default_token_budget(mode: str) -> tuple[int, int]:
    """The ``(max_tokens, context_tokens)`` defaults for an inference
    mode, used when ``INFERENCE_MAX_TOKENS`` / ``INFERENCE_CONTEXT_TOKENS``
    are unset."""
    if mode == "anthropic":
        return ANTHROPIC_DEFAULT_MAX_TOKENS, ANTHROPIC_DEFAULT_CONTEXT_TOKENS
    return DEFAULT_MAX_TOKENS, DEFAULT_CONTEXT_TOKENS


# Characters per token assumed when counting a prompt. mcp-server ships
# no tokenizer, so a prompt's length is estimated from its characters.
# English prose averages about four characters per token on current
# BPE tokenizers; three over-counts it by about a third, the safety
# margin. Text that tokenizes more densely (CJK scripts, long digit or
# base64 runs) can still exceed the estimate; the provider then stops
# the reply or rejects the prompt, as it does without this count.
CHARS_PER_TOKEN = 3

# Tokens set aside for the chat template around the system and user
# messages (role markers, separators), which the character count of the
# two messages does not see.
TEMPLATE_RESERVE_TOKENS = 64

# Fewest prompt tokens a configuration may leave after the reply and
# template reserves. The longest system prompt (brief_issue's) alone is
# about 940 estimated tokens, so a smaller allowance is a misconfiguration.
MIN_PROMPT_TOKENS = 1024

# Fixed text for a provider 400 on a call that carried a structured-output
# schema (#808). The provider's message is never parsed (it can echo the
# prompt), so the text lists the common causes in order and names the
# setting only for the last one (#812). There is no retry without the
# format, so the operator decides.
STRUCTURED_OUTPUT_REJECTED = (
    "Inference provider rejected a structured-output request with status 400 "
    "(mode=anthropic). Common causes, in order: account credit or billing; "
    "an invalid or retired INFERENCE_MODEL; a prompt too large for the "
    "model's window (check INFERENCE_CONTEXT_TOKENS); or a model or gateway "
    "without structured outputs, in which case set "
    "INFERENCE_STRUCTURED_OUTPUT=false."
)


@dataclass(frozen=True)
class PromptBudget:
    """How much prompt text one inference call may carry.

    ``context_tokens`` is the model's window (``INFERENCE_CONTEXT_TOKENS``)
    and ``max_output_tokens`` the reply reserve (``INFERENCE_MAX_TOKENS``).
    What remains after them and ``TEMPLATE_RESERVE_TOKENS`` is the prompt
    allowance, counted in characters at ``CHARS_PER_TOKEN``: a prompt of
    at most ``prompt_chars`` characters is estimated at no more than
    ``prompt_tokens`` tokens.
    """

    context_tokens: int = DEFAULT_CONTEXT_TOKENS
    max_output_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        if self.prompt_tokens < MIN_PROMPT_TOKENS:
            raise ValueError(
                f"INFERENCE_CONTEXT_TOKENS ({self.context_tokens}) must exceed "
                f"INFERENCE_MAX_TOKENS ({self.max_output_tokens}) by at least "
                f"{MIN_PROMPT_TOKENS + TEMPLATE_RESERVE_TOKENS} tokens so a prompt fits"
            )

    @property
    def prompt_tokens(self) -> int:
        return self.context_tokens - self.max_output_tokens - TEMPLATE_RESERVE_TOKENS

    @property
    def prompt_chars(self) -> int:
        return self.prompt_tokens * CHARS_PER_TOKEN


def estimate_tokens(text: str) -> int:
    """Estimated tokens in ``text`` at ``CHARS_PER_TOKEN``, rounded up."""
    return -(-len(text) // CHARS_PER_TOKEN)


# Why a reply stopped early (``InferenceTruncatedError.reason``).
TruncationReason = Literal["max_tokens", "context_window"]


class InferenceTruncatedError(ProviderResponseError):
    """The model stopped at ``max_tokens`` before finishing its answer.

    ``partial`` holds whatever text it produced. Callers decide what a
    cut-off answer is worth: prose can be shown with a notice, while a
    cut-off JSON record is a failed extraction, never an empty one. The
    message itself carries no response content, so it is safe to log.

    ``reason`` is a fixed value naming the stop: ``max_tokens`` (the
    reply reached ``INFERENCE_MAX_TOKENS``) or ``context_window``
    (Anthropic's ``model_context_window_exceeded``: the model's own
    window filled first). The message is the same for both, so what the
    caller sees does not change; the reason is for the server log.
    """

    def __init__(self, partial: str, reason: TruncationReason = "max_tokens") -> None:
        super().__init__(
            "Inference output hit the max_tokens limit before finishing "
            "(raise INFERENCE_MAX_TOKENS)"
        )
        self.partial = partial
        self.reason: TruncationReason = reason


class _Backend(Protocol):
    """Structural contract every inference backend satisfies.

    Defined as a ``Protocol`` (not an inheritance base) so future
    backends and test fakes can stay duck-typed without depending on
    any specific SDK.

    ``base_url`` is the wire endpoint after the SDK resolved its
    fallback chain (so the empty-string operator input becomes the
    SDK default literal, not ``""``). ``InferenceClient`` re-exposes
    it so ``main.py`` can log the resolved URL alongside the embed
    and rerank lines — important in a privacy-sensitive deployment
    where the operator needs the startup log to name exactly where
    retrieved email excerpts are being sent.

    ``structured_output`` says whether a ``json_schema`` passed to
    ``complete`` is sent to the provider as a structured-output format
    (#808); a backend that never sends one sets it ``False``.
    """

    base_url: str
    structured_output: bool

    async def complete(self, system: str, user: str, json_schema: dict | None = None) -> str: ...


class _OpenAIBackend:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        max_tokens: int,
        timeout_secs: float,
    ) -> None:
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient

        self.model = model
        self.max_tokens = max_tokens
        # Structured outputs are not sent in openai mode (#807): support for
        # ``response_format`` differs across the servers this mode targets.
        self.structured_output = False
        # ``api_key`` is required (non-empty) — startup validation in
        # ``main.py`` rejects an empty value before reaching this
        # constructor. For unauthenticated host-side servers (LM Studio, vLLM,
        # ``mlx_lm.server``) the operator supplies any placeholder
        # string; compat servers ignore the bearer header. Keeping the
        # substitution out of this constructor means the audit trail
        # of "what did we send as the credential" is exactly what the
        # operator wrote — no silent rewrite to a literal that could
        # surface in a misconfigured remote provider's request log.
        #
        # ``base_url`` may be empty: an empty value omits the kwarg so
        # the SDK's documented fallback chain fires
        # (``OPENAI_BASE_URL`` env → ``https://api.openai.com/v1``
        # literal). Symmetric with the ``_AnthropicBackend`` empty-URL
        # path and with ``EmbedClient``. Passing an empty string through
        # would defeat the fallback because the SDK only treats
        # ``None`` as "missing." ``main.py`` passes an empty value only
        # for an explicit ``INFERENCE_BASE_URL=default``; an empty
        # ``INFERENCE_BASE_URL`` fails startup (#750).
        #
        # ``max_retries=0`` disables SDK-internal retries so one
        # ``complete()`` call makes one request and ``timeout_secs`` is
        # not multiplied by retries. It is a per-operation HTTP timeout
        # (each connect, read or write must make progress within it),
        # not a total-call deadline: a provider that streams a response
        # in small fragments can keep the call alive past it (#287).
        # Default SDK posture (2 retries + exponential backoff) would
        # silently turn a stall on ``INFERENCE_TIMEOUT_SECS=300`` into
        # a ~15 min hang — hostile to operators tuning the timeout and
        # to the calling agent, which loses context long before the SDK
        # gives up. On a
        # transient 5xx the tool surfaces a clean error and the agent
        # (or user) can re-invoke. Parity with ``OpenAIEmbedder`` in the
        # indexer, which also pins ``max_retries=0`` (it owns retries
        # above via tenacity; mcp-server has no higher retry layer and
        # deliberately doesn't add one).
        #
        # The SDK re-sends the body on a redirect; the request hook
        # refuses a hop off the resolved endpoint's origin (#340).
        same_origin_only = same_origin_request_hook(
            lambda: self.client.base_url, "Inference provider (mode=openai)", log
        )
        http_client = DefaultAsyncHttpxClient(event_hooks={"request": [same_origin_only]})
        if base_url:
            self.client = AsyncOpenAI(
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                timeout=timeout_secs,
                max_retries=0,
                http_client=http_client,
            )
        else:
            self.client = AsyncOpenAI(
                api_key=api_key,
                timeout=timeout_secs,
                max_retries=0,
                http_client=http_client,
            )
        # After the SDK resolves its fallback chain, read the URL back
        # so ``self.base_url`` always reflects the wire endpoint —
        # useful for log lines that name what the backend is actually
        # talking to.
        self.base_url = str(self.client.base_url).rstrip("/")

    async def complete(self, system: str, user: str, json_schema: dict | None = None) -> str:
        # ``json_schema`` is ignored: openai mode is #807.
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self.max_tokens,
            stream=False,
        )
        # OpenAI-compatible servers occasionally return empty
        # ``choices`` (provider error states, content-filter trips) or a
        # ``message.content`` of ``None``/``""`` (tool-call-only deltas,
        # length-truncated responses). Returning the empty string would
        # surface to the caller as a silent blank answer — the
        # intelligence tools then pass that straight through to the
        # agent, which has no signal that the provider failed. Raise so
        # the operator-facing log line names the failure mode. The
        # message contains no prompt or response content, so logging
        # the exception cannot leak user data.
        if not resp.choices:
            raise ProviderResponseError("Inference provider returned no choices (mode=openai)")
        content = resp.choices[0].message.content
        # The SDK does not validate ``content`` on a 200, so a provider
        # can hand back a JSON object, list or number here (#321). Reject
        # it before it becomes an answer or a truncated partial; the
        # message never quotes the value.
        if not isinstance(content, str | None):
            raise ProviderResponseError(
                "Inference provider returned non-text content (mode=openai)"
            )
        finish_reason = getattr(resp.choices[0], "finish_reason", None)
        if finish_reason == "length":
            raise InferenceTruncatedError(content or "")
        if finish_reason == "content_filter":
            # The provider stopped the answer part-way; its prefix must
            # not pass as a finished answer.
            raise ProviderResponseError(
                "Inference provider stopped the answer with a content filter (mode=openai)"
            )
        if content is None or not content.strip():
            raise ProviderResponseError("Inference provider returned empty content (mode=openai)")
        return content


class _AnthropicBackend:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        max_tokens: int,
        timeout_secs: float,
        structured_output: bool = True,
    ) -> None:
        from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

        self.model = model
        self.max_tokens = max_tokens
        # INFERENCE_STRUCTURED_OUTPUT (#808): send a caller's JSON schema as
        # ``output_config.format``. Off for a model or gateway without
        # structured outputs.
        self.structured_output = structured_output
        # The Anthropic SDK appends ``/v1/messages`` to ``base_url``
        # itself, so a base URL such as ``https://api.anthropic.com/v1``
        # would produce a request to ``.../v1/v1/messages`` — every
        # intelligence tool 404s with an opaque SDK error. Reject the
        # ``/v1`` suffix at construction so the operator gets a clear
        # configuration message instead of a runtime mystery. Stripping
        # silently would hide the misconfiguration; rejecting forces
        # the operator to confirm they meant the SDK base, not a
        # versioned path.
        stripped = base_url.rstrip("/") if base_url else ""
        if stripped.endswith("/v1"):
            raise ValueError(
                "INFERENCE_BASE_URL must not end with '/v1' when "
                "INFERENCE_MODE=anthropic — the Anthropic SDK appends "
                "'/v1/messages' itself. Drop the trailing '/v1' "
                "(e.g. use 'https://api.anthropic.com', or set it to "
                "'default' to use the SDK default)."
            )

        # Pass ``base_url`` only when explicitly set so the SDK's real
        # default URL is used when the operator set
        # ``INFERENCE_BASE_URL=default`` (``main.py`` resolves it to "").
        # Passing an empty string would override the SDK default with a
        # malformed URL. ``max_retries=0`` for the same reason as
        # ``_OpenAIBackend``: retries must not multiply ``timeout_secs``,
        # which is a per-operation HTTP timeout, not a total-call
        # deadline.
        #
        # The SDK's HTTP client follows redirects and re-sends the body
        # and ``x-api-key`` to wherever ``Location`` points (#325). The
        # request hook runs before every hop, so a redirect off the
        # resolved endpoint's origin is refused before the prompt or
        # key leaves; same-origin redirects still work.
        same_origin_only = same_origin_request_hook(
            lambda: self.client.base_url, "Inference provider (mode=anthropic)", log
        )
        http_client = DefaultAsyncHttpxClient(event_hooks={"request": [same_origin_only]})
        if stripped:
            self.client = AsyncAnthropic(
                base_url=stripped,
                api_key=api_key,
                timeout=timeout_secs,
                max_retries=0,
                http_client=http_client,
            )
        else:
            self.client = AsyncAnthropic(
                api_key=api_key,
                timeout=timeout_secs,
                max_retries=0,
                http_client=http_client,
            )
        # After the SDK resolves its fallback chain, read the URL back
        # so ``self.base_url`` always reflects the wire endpoint — the
        # same pattern used by ``_OpenAIBackend``, ``EmbedClient``,
        # and ``OpenAIEmbedder``. Useful for diagnostic log lines that
        # name what the backend is actually talking to.
        self.base_url = str(self.client.base_url).rstrip("/")

    async def complete(self, system: str, user: str, json_schema: dict | None = None) -> str:
        if json_schema is None or not self.structured_output:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        else:
            from anthropic import BadRequestError

            try:
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_config={"format": {"type": "json_schema", "schema": json_schema}},
                )
            except BadRequestError as e:
                # A model or gateway without structured outputs rejects the
                # format. Log type and status only (the body can echo the
                # prompt), keep the SDK error out of the chain, and name the
                # setting; there is no retry without the format (#808).
                log.warning(
                    "Inference provider rejected a structured-output request: %s",
                    safe_provider_exception_text(e),
                )
                raise ProviderResponseError(STRUCTURED_OUTPUT_REJECTED) from None
        # The Messages API returns a list of content blocks. Concatenate
        # every text block so a future model that emits multiple text
        # blocks (or thinking + text) lands the full answer rather than
        # silently dropping all but the first. Non-text blocks
        # (``tool_use``, ``thinking``) are skipped by the type check.
        # ``getattr`` with a default makes the lookup total over the
        # union of block types without requiring an isinstance ladder
        # for every Anthropic block subclass.
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and getattr(block, "type", None) == "text":
                parts.append(text)
        result = "".join(parts)
        stop_reason = getattr(resp, "stop_reason", None)
        if stop_reason == "max_tokens":
            raise InferenceTruncatedError(result)
        if stop_reason == "model_context_window_exceeded":
            raise InferenceTruncatedError(result, reason="context_window")
        if stop_reason == "refusal":
            raise ProviderResponseError("Inference provider refused to answer (mode=anthropic)")
        # A blank result means the response contained no answer text
        # (empty ``content``, only ``tool_use`` / ``thinking`` blocks, or
        # whitespace-only text blocks). Returning it would let the caller
        # pass a silent blank answer to the agent; raise so the failure
        # surfaces with a clear, sanitized error (no prompt/response
        # content) instead.
        # Structured callers that expected JSON get a ProviderResponseError here
        # rather than a JSONDecodeError two layers down.
        if not result.strip():
            raise ProviderResponseError(
                "Inference provider returned no text blocks (mode=anthropic)"
            )
        return result


class InferenceClient:
    """Mode-dispatching inference client.

    Instantiate with ``InferenceClient.create(mode, base_url, model,
    api_key)``; the factory raises if the mode is unknown so all
    branches are total. ``mode="none"`` is handled in ``main.py`` —
    this class is only constructed for an active mode.
    """

    def __init__(self, backend: _Backend, mode: str) -> None:
        self._backend = backend
        self.mode = mode
        # Re-expose the backend's resolved wire endpoint so the startup
        # log line in ``main.py`` can name the exact URL retrieved email
        # excerpts are sent to. Mirrors ``EmbedClient.base_url`` — the
        # empty-string operator input is replaced with the SDK's
        # resolved default ("https://api.anthropic.com" /
        # "https://api.openai.com/v1") rather than logged as "SDK default."
        self.base_url = backend.base_url
        # Whether a ``json_schema`` passed to ``complete`` is sent as a
        # structured-output format: anthropic mode with
        # INFERENCE_STRUCTURED_OUTPUT on (#808). Tools read it to word
        # their prompt and parse the reply to match.
        self.structured_output = backend.structured_output

    @classmethod
    def create(
        cls,
        *,
        mode: str,
        base_url: str,
        model: str,
        api_key: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_secs: float = DEFAULT_COMPLETE_TIMEOUT_SECS,
        structured_output: bool = True,
    ) -> InferenceClient:
        if mode == "openai":
            return cls(
                _OpenAIBackend(
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    max_tokens=max_tokens,
                    timeout_secs=timeout_secs,
                ),
                mode,
            )
        if mode == "anthropic":
            return cls(
                _AnthropicBackend(
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    max_tokens=max_tokens,
                    timeout_secs=timeout_secs,
                    structured_output=structured_output,
                ),
                mode,
            )
        raise ValueError(f"InferenceClient: unsupported mode {mode!r}")

    async def complete(self, system: str, user: str, *, json_schema: dict | None = None) -> str:
        """The model's reply. ``json_schema`` (a strict JSON schema whose
        top level is an object) is sent as a structured-output format when
        ``structured_output`` is on, and ignored otherwise."""
        return await self._backend.complete(system, user, json_schema)
