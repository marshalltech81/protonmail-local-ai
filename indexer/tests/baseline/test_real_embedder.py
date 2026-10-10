"""Tests for the real-embedder baseline build (#1439).

The provider is the loopback hash-embedding service (``embed_server``),
so nothing leaves the machine and nothing is spent.
"""

import contextlib
import json
import signal
import socket
import stat
import threading
import time
from pathlib import Path

import httpx2
import pytest
from openai import APIStatusError
from src.database import EMBEDDING_DIM
from src.embedder import OpenAIEmbedder

from tests.baseline import embed_server
from tests.baseline import real_embedder as re_mod
from tests.baseline.real_embedder import (
    ARGUMENTS,
    EXIT_FAILED,
    EXIT_INCONCLUSIVE,
    EXIT_USAGE,
    KEY_FILE,
    CachedEmbedder,
    EmbedBudgetExhausted,
    RequestMeter,
    UsageError,
    VectorCache,
    main_cli,
    measure_variation,
    parse,
    validate,
)
from tests.baseline.test_baseline_build import _GOLDEN, requires_ocr

_KEY = "KEYMARKER-1439-not-a-real-key"
_URL_MARK = "URLMARKER1439"


def _secrets(path: Path, content: str = _KEY, mode: int = 0o600) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    key = path / KEY_FILE
    key.write_text(content, encoding="utf-8")
    key.chmod(mode)
    return path


def _valid_raw(tmp_path: Path) -> dict[str, str | None]:
    return {
        "out_dir": str(tmp_path / "out"),
        "golden": str(_GOLDEN),
        "--cache-dir": str(tmp_path / "cache"),
        "--secrets-dir": str(_secrets(tmp_path / "secrets")),
        "--repeats": None,
        "--max-requests": None,
        "--batch-size": None,
        "EMBED_BASE_URL": "http://127.0.0.1:9/v1",
        "EMBED_MODEL": "some-model",
        "EMBED_MODE": None,
    }


# ---- the argument table ---------------------------------------------------
#
# Each case is (argument, value, accepted). ``@...`` values are built in
# tmp_path by ``_materialise``. Cases come from the table's rows by kind,
# so a new row is walked as soon as it is added; a row of a new kind
# fails ``test_every_argument_kind_is_walked``.


def _kind_cases(arg):
    match arg.kind:
        case "int":
            return [
                (str(arg.minimum), True),
                (str(arg.maximum), True),
                (str(arg.minimum - 1), False),
                (str(arg.maximum + 1), False),
                ("two", False),
                ("1.5", False),
                ("-1", False),
                (None, arg.default is not None),
            ]
        case "text":
            return [("x", True), ("", False), ("   ", False), (None, False)]
        case "choice":
            return [
                *((c, True) for c in arg.choices),
                *((c.upper(), True) for c in arg.choices),
                ("other", False),
                (None, arg.default is not None),
            ]
        case "url":
            return [
                ("https://provider.example/v1", True),
                ("default", True),
                ("DEFAULT", True),
                ("", False),
                ("ftp://provider.example/v1", False),
                ("not a url", False),
                (f"https://user:{_URL_MARK}@provider.example/v1", False),
                # Review round 3: a query string or fragment could carry a
                # credential into an error message, and only containers
                # resolve host.docker.internal.
                (f"https://provider.example/v1?key={_URL_MARK}", False),
                (f"https://provider.example/v1#{_URL_MARK}", False),
                ("http://host.docker.internal:8001/v1", False),
                ("http://127.0.0.1:8001/v1", True),
            ]
        case "new_dir":
            return [
                ("@missing", True),
                ("@empty_dir", True),
                ("@full_dir", False),
                ("@file", False),
            ]
        case "file":
            return [("@file", True), ("@missing", False), ("@empty_dir", False), (None, False)]
        case "cache_dir":
            return [("@missing", True), ("@empty_dir", True), ("@file", False), (None, False)]
        case "secrets_dir":
            return [
                ("@key600", True),
                ("@key644", False),
                ("@key_empty", False),
                ("@empty_dir", False),
                (None, False),
            ]
    return []


ARG_CASES = [
    pytest.param(arg.name, value, ok, id=f"{arg.name}-{value}-{'ok' if ok else 'bad'}")
    for arg in ARGUMENTS
    for value, ok in _kind_cases(arg)
]


def _materialise(value: str | None, tmp_path: Path) -> str | None:
    if value is None or not value.startswith("@"):
        return value
    target = tmp_path / "case"
    match value:
        case "@missing":
            pass
        case "@empty_dir":
            target.mkdir()
        case "@full_dir":
            target.mkdir()
            (target / "x").write_text("x")
        case "@file":
            target.write_text("x")
        case "@key600":
            _secrets(target)
        case "@key644":
            _secrets(target, mode=0o644)
        case "@key_empty":
            _secrets(target, content="  \n")
    return str(target)


def test_every_argument_kind_is_walked():
    assert all(_kind_cases(arg) for arg in ARGUMENTS)
    # The table is the only place arguments are declared: the parser
    # builds from it, and validate reads nothing else.
    assert len({arg.name for arg in ARGUMENTS}) == len(ARGUMENTS)


@pytest.mark.parametrize(("name", "value", "ok"), ARG_CASES)
def test_argument_table(tmp_path, name, value, ok):
    raw = _valid_raw(tmp_path)
    raw[name] = _materialise(value, tmp_path)
    if ok:
        validate(raw)
        return
    with pytest.raises(UsageError) as info:
        validate(raw)
    message = str(info.value)
    assert name in message or KEY_FILE in message
    # Errors never echo a value: a URL could carry credentials.
    assert _URL_MARK not in message and _KEY not in message


def test_validate_reports_every_failure_at_once(tmp_path):
    raw = _valid_raw(tmp_path)
    raw.update({"--repeats": "1", "--max-requests": "0", "EMBED_MODEL": ""})
    with pytest.raises(UsageError) as info:
        validate(raw)
    assert all(n in str(info.value) for n in ("--repeats", "--max-requests", "EMBED_MODEL"))


def test_validate_applies_defaults_and_resolves_default_url(tmp_path):
    raw = _valid_raw(tmp_path)
    raw["EMBED_BASE_URL"] = "default"
    settings = validate(raw)
    assert (settings.repeats, settings.max_requests, settings.batch_size) == (2, 250, 64)
    assert settings.max_runtime_secs == 1800
    assert settings.base_url == "https://api.openai.com/v1"
    assert settings.api_key == _KEY
    assert _KEY not in repr(settings)


def test_parse_reads_flags_positionals_and_env(tmp_path):
    secrets = _secrets(tmp_path / "secrets")
    settings = parse(
        [
            str(tmp_path / "out"),
            str(_GOLDEN),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--secrets-dir",
            str(secrets),
            "--repeats",
            "3",
        ],
        {"EMBED_BASE_URL": "https://provider.example/v1", "EMBED_MODEL": "m"},
    )
    assert settings.repeats == 3 and settings.model == "m"


def test_runtime_cap_bounds_the_run_and_reports_inconclusive(tmp_path, monkeypatch, capsys):
    """Review round 3: the table's runtime cap reaches the deadline, and
    reaching it is inconclusive, never a pass."""
    seen = []

    @contextlib.contextmanager
    def deadline(seconds):
        seen.append(seconds)
        yield

    def run(settings, meter):
        raise EmbedBudgetExhausted("the runtime cap (90 s)")

    monkeypatch.setattr(re_mod, "deadline", deadline)
    monkeypatch.setattr(re_mod, "run", run)
    raw = _valid_raw(tmp_path)
    argv = [raw["out_dir"], raw["golden"], "--cache-dir", raw["--cache-dir"]]
    argv += ["--secrets-dir", raw["--secrets-dir"], "--max-runtime-secs", "90"]
    env = {"EMBED_BASE_URL": "http://127.0.0.1:9/v1", "EMBED_MODEL": "m"}
    assert main_cli(argv, env) == EXIT_INCONCLUSIVE
    assert seen == [90]
    err = capsys.readouterr().err
    assert "INCONCLUSIVE: the runtime cap (90 s) was reached" in err


def test_deadline_interrupts_a_response_that_never_finishes():
    """A provider that keeps a response alive by trickling bytes never
    trips the per-operation HTTP timeout; the deadline still ends the
    request, and the alarm is cleared afterwards."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()
    trickled = []

    def serve():
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
            while not stop.is_set():
                conn.sendall(b" ")
                trickled.append(1)
                time.sleep(0.1)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/v1"
    embedder = OpenAIEmbedder(url, "m", api_key="unauthenticated", request_timeout=5.0)
    started = time.monotonic()
    try:
        with pytest.raises(EmbedBudgetExhausted, match="runtime cap"):
            with re_mod.deadline(1):
                embedder.embed_batch(["synthetic"])
    finally:
        stop.set()
        embedder.client.close()
        thread.join(timeout=5)
        listener.close()
    elapsed = time.monotonic() - started
    # Bytes kept arriving past the 5 s read timeout's reach, yet the run
    # ended at the deadline.
    assert elapsed < 4 and len(trickled) >= 5
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_usage_error_exits_2_without_echoing_values(tmp_path, capsys):
    argv = [str(tmp_path / "out"), str(_GOLDEN), "--cache-dir", str(tmp_path / "c")]
    env = {"EMBED_BASE_URL": f"https://u:{_URL_MARK}@h.example/v1", "EMBED_MODEL": "m"}
    assert main_cli(argv, env) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "--secrets-dir is required" in err and _URL_MARK not in err


# ---- metering and caching --------------------------------------------------


class _Usage:
    def __init__(self, tokens):
        self.prompt_tokens = tokens


class _Response:
    def __init__(self, tokens):
        self.usage = None if tokens is None else _Usage(tokens)


def test_meter_counts_requests_and_tokens_and_refuses_past_the_cap():
    sent = []
    meter = RequestMeter(max_requests=2)
    create = meter.wrap(lambda **kw: sent.append(kw) or _Response(7))
    create(input=["a"])
    create(input=["b"])
    with pytest.raises(EmbedBudgetExhausted):
        create(input=["c"])
    assert len(sent) == 2 and meter.requests == 2 and meter.input_tokens == 14
    # Not an Exception: the indexer's handlers cannot absorb it.
    assert not issubclass(EmbedBudgetExhausted, Exception)


def test_meter_notes_requests_without_usage():
    meter = RequestMeter(max_requests=5)
    meter.wrap(lambda **kw: _Response(None))()
    assert (meter.requests, meter.input_tokens, meter.unreported) == (1, 0, 1)


def test_meter_counts_a_failed_attempt_as_unreported_usage():
    """Review round 2: an attempt that raised may still have been
    processed (and billed) without returning usage, so the token total
    is no longer complete."""

    def fail(**kw):
        raise ConnectionError("synthetic")

    meter = RequestMeter(max_requests=5)
    with pytest.raises(ConnectionError):
        meter.wrap(fail)()
    meter.wrap(lambda **kw: _Response(4))()
    assert (meter.requests, meter.input_tokens, meter.unreported) == (2, 4, 1)


class _Inner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed_batch(self, texts, *, on_batch_complete=None):
        self.calls.append(list(texts))
        if on_batch_complete is not None:
            on_batch_complete()
        return [[float(len(t)), 1.0] for t in texts]


_IDENTITY = {"endpoint": "http://127.0.0.1/v1", "model": "m", "batch_size": 64}


def test_cache_serves_a_repeated_call_and_re_embeds_a_changed_batch(tmp_path):
    """A chunk vector is cached under the whole call it was sent in
    (Codex round 1): the provider's output can depend on a text's batch
    neighbours, so a call that differs in any text, or in order, is
    sent again whole, as a fresh build would send it."""
    cache = VectorCache(tmp_path / "cache", _IDENTITY)
    inner = _Inner()
    embedder = CachedEmbedder(inner.embed_batch, cache, repeat=1)
    assert embedder.embed_batch(["aa", "b"]) == [[2.0, 1.0], [1.0, 1.0]]
    done = []
    assert embedder.embed_batch(["aa", "b"], on_batch_complete=lambda: done.append(1)) == [
        [2.0, 1.0],
        [1.0, 1.0],
    ]
    assert inner.calls == [["aa", "b"]] and done == [1]
    # One new neighbour, or the same texts reordered: every text is sent.
    embedder.embed_batch(["aa", "b", "ccc"])
    embedder.embed_batch(["b", "aa"])
    assert inner.calls == [["aa", "b"], ["aa", "b", "ccc"], ["b", "aa"]]
    assert (embedder.hits, embedder.misses) == (2, 7)
    assert len(embedder.items) == 7
    assert stat.S_IMODE((tmp_path / "cache").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "cache" / "vectors.sqlite").stat().st_mode) == 0o600
    cache.close()


@pytest.mark.parametrize(
    "change",
    [
        {"repeat": 2},
        {"text": "other"},
        *({"identity": {**_IDENTITY, key: "changed"}} for key in _IDENTITY),
    ],
)
def test_cache_key_covers_text_repeat_and_identity(tmp_path, change):
    caches = [
        VectorCache(tmp_path / "a", _IDENTITY),
        VectorCache(tmp_path / "b", _IDENTITY),
        VectorCache(tmp_path / "c", change.get("identity", _IDENTITY)),
    ]
    base, same, other = caches
    try:
        assert base.key(1, "text") == same.key(1, "text")
        assert base.key(1, "text") != other.key(change.get("repeat", 1), change.get("text", "text"))
    finally:
        for cache in caches:
            cache.close()


@pytest.mark.parametrize(
    "url",
    [v for a in ARGUMENTS if a.kind == "url" for v, ok in _kind_cases(a) if ok and "://" in v],
)
def test_identity_endpoint_is_the_whole_accepted_url(tmp_path, url):
    """The cache identity names the endpoint through ``sanitize_endpoint``;
    every URL the table accepts survives it whole (review rounds 2-3),
    so two destinations never share cached vectors."""
    raw = _valid_raw(tmp_path)
    raw["EMBED_BASE_URL"] = url
    settings = validate(raw)
    assert re_mod.embedding_identity(settings, "chunk")["endpoint"] == url.rstrip("/")


def test_identity_names_every_setting_that_decides_a_vector(tmp_path):
    settings = validate(_valid_raw(tmp_path))
    chunk = re_mod.embedding_identity(settings, "chunk")
    query = re_mod.embedding_identity(settings, "query")
    expected = {
        "role",
        "endpoint",
        "model",
        "dimensions",
        "encoding_format",
        "openai_sdk",
        "input",
        "batch_size",
        "normalization",
    }
    assert set(chunk) == set(query) == expected
    assert (chunk["input"], chunk["batch_size"], chunk["normalization"]) == ("list", 64, "l2")
    assert (query["input"], query["batch_size"], query["normalization"]) == ("string", 1, "none")


class _Data:
    def __init__(self, embedding):
        self.embedding = embedding


class _Embeddings:
    def __init__(self, vectors, failures=()):
        self.vectors = vectors
        self.failures = list(failures)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        response = _Response(3)
        response.data = [_Data(v) for v in self.vectors]
        return response


class _Client:
    def __init__(self, vectors, failures=()):
        self.embeddings = _Embeddings(vectors, failures)


class _FakeEmbedder:
    def __init__(self, vectors, failures=()):
        self.model = "m"
        self.client = _Client(vectors, failures)


def _status(code: int, retry_after: str | None = None) -> APIStatusError:
    headers = {} if retry_after is None else {"retry-after": retry_after}
    return APIStatusError(
        message=str(code),
        response=httpx2.Response(code, headers=headers, request=httpx2.Request("POST", "http://x")),
        body=None,
    )


def test_query_sender_sends_each_query_alone_as_a_string_unnormalised():
    raw = [3.0, 4.0] + [0.0] * (EMBEDDING_DIM - 2)
    fake = _FakeEmbedder([raw])
    done = []
    send = re_mod.query_sender(fake, RequestMeter(10))
    vectors = send(["one", "two"], on_batch_complete=lambda: done.append(1))
    assert vectors == [raw, raw]
    assert fake.client.embeddings.requests == [
        {"model": "m", "input": "one"},
        {"model": "m", "input": "two"},
    ]
    assert done == [1]


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[1.0] * EMBEDDING_DIM, [1.0] * EMBEDDING_DIM],
        [[float("nan")] * EMBEDDING_DIM],
        # Review round 2: as mcp-server's ``embed_query`` rejects them.
        [[0.0] * EMBEDDING_DIM],
        [[1.0] * (EMBEDDING_DIM - 1)],
    ],
    ids=["none", "two", "non-finite", "all-zero", "wrong-width"],
)
def test_query_sender_rejects_a_malformed_response(vectors):
    with pytest.raises(re_mod.EmbedResponseError):
        re_mod.query_sender(_FakeEmbedder(vectors), RequestMeter(10))(["q"])


def test_query_sender_retries_a_transient_failure_and_counts_it():
    vector = [1.0] * EMBEDDING_DIM
    fake = _FakeEmbedder([vector], failures=[_status(429, "7"), _status(503)])
    meter = RequestMeter(10)
    waits: list[float] = []
    assert re_mod.query_sender(fake, meter, sleep=waits.append)(["q"]) == [vector]
    # Retry-After when sent, else the backoff; three attempts in all.
    assert waits == [7.0, 4.0] and meter.retries == 2
    assert len(fake.client.embeddings.requests) == 3


@pytest.mark.parametrize(
    ("failures", "retries"),
    [
        ([_status(401)], 0),  # a configuration error is not retried
        ([_status(429), _status(429), _status(429)], 2),  # gives up after three attempts
    ],
)
def test_query_sender_raises_after_its_attempts(failures, retries):
    fake = _FakeEmbedder([[1.0]], failures=failures)
    meter = RequestMeter(10)
    with pytest.raises(APIStatusError):
        re_mod.query_sender(fake, meter, sleep=lambda _s: None)(["q"])
    assert meter.retries == retries


def test_one_at_a_time_caches_each_vector_before_the_next_request(tmp_path):
    cache = VectorCache(tmp_path / "cache", _IDENTITY)
    sent: list[list[str]] = []

    def send(texts, *, on_batch_complete=None):
        sent.append(list(texts))
        if texts == ["b"]:
            raise EmbedBudgetExhausted("the request cap (1)")
        return [[1.0, 0.0] for _ in texts]

    embedder = CachedEmbedder(send, cache, repeat=1, one_at_a_time=True)
    with pytest.raises(EmbedBudgetExhausted):
        embedder.embed_batch(["a", "b"])
    assert sent == [["a"], ["b"]]
    # "a" was paid for and kept, under a key that ignores its neighbours
    # (each query is sent alone).
    assert sent == [["a"], ["b"]]
    sent.clear()
    assert embedder.embed_batch(["z", "a"]) == [[1.0, 0.0], [1.0, 0.0]]
    assert sent == [["z"]]
    cache.close()


def test_variation_compares_every_later_repeat_with_the_first(tmp_path):
    cache = VectorCache(tmp_path / "cache", _IDENTITY)
    cache.put_many(
        [
            (cache.key(1, "a"), [1.0, 0.0]),
            (cache.key(2, "a"), [1.0, 0.0]),
            (cache.key(3, "a"), [0.0, 1.0]),
            (cache.key(1, "b"), [1.0, 0.0]),
            (cache.key(2, "b"), [1.0, 0.0]),
        ]
    )
    got = measure_variation(cache, [{"a", "b"}, {"a", "b"}, {"a"}])
    assert got == {
        "pairs": 3,
        "max_cosine_distance": 1.0,
        "mean_cosine_distance": pytest.approx(1 / 3),
        "pairs_not_identical": 1,
    }
    cache.close()


@pytest.mark.parametrize(
    ("error", "shown", "hidden"),
    [
        (RuntimeError("baseline corpus did not index cleanly: {'dead': 1}"), "dead", None),
        (ValueError("MAILMARKER echoed by a parser"), "ValueError", "MAILMARKER"),
    ],
)
def test_failure_exits_1_with_fixed_text(tmp_path, monkeypatch, capsys, error, shown, hidden):
    def fail(settings, meter):
        raise error

    monkeypatch.setattr(re_mod, "run", fail)
    raw = _valid_raw(tmp_path)
    argv = [raw["out_dir"], raw["golden"], "--cache-dir", raw["--cache-dir"]]
    argv += ["--secrets-dir", raw["--secrets-dir"]]
    env = {"EMBED_BASE_URL": "http://127.0.0.1:9/v1", "EMBED_MODEL": "m"}
    assert main_cli(argv, env) == EXIT_FAILED
    err = capsys.readouterr().err
    assert "FAILED" in err and shown in err
    assert hidden is None or hidden not in err


# ---- end to end against the loopback service --------------------------------


@pytest.fixture
def provider(monkeypatch):
    """The loopback hash service, counting the requests it answers."""
    answered = []
    real = embed_server.embeddings_response

    def counting(body):
        answered.append(body)
        return real(body)

    monkeypatch.setattr(embed_server, "embeddings_response", counting)
    with embed_server.serve() as url:
        yield url, answered


def _argv(tmp_path: Path, out: str, *extra: str) -> list[str]:
    return [
        str(tmp_path / out),
        str(_GOLDEN),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--secrets-dir",
        str(_secrets(tmp_path / "secrets")),
        *extra,
    ]


@requires_ocr
def test_builds_every_repeat_then_reruns_from_the_cache(tmp_path, provider, capsys):
    url, answered = provider
    env = {"EMBED_BASE_URL": url, "EMBED_MODEL": embed_server.MODEL}
    assert main_cli(_argv(tmp_path, "run1"), env) == 0
    report = json.loads((tmp_path / "run1" / "run.json").read_text(encoding="utf-8"))
    assert report["requests"] == len(answered) > 0
    assert report["cache_hits"] == 0 and report["cache_misses"] > 0
    for repeat in (1, 2):
        built = tmp_path / "run1" / f"repeat-{repeat}"
        assert (built / "mail.db").is_file()
        vectors = json.loads((built / "query_vectors.json").read_text(encoding="utf-8"))
        golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        assert {q["query"] for q in golden["semantic"]} <= set(vectors)
    # The hash service is deterministic: every text compared, none differ.
    assert set(report["variation"]) == {"chunks", "queries"}
    for variation in report["variation"].values():
        assert variation["pairs"] > 0 and variation["pairs_not_identical"] == 0
    # Every query went alone, as a string; chunks went in lists.
    inputs = [body["input"] for body in answered]
    queries = sum(isinstance(i, str) for i in inputs)
    # Two repeats: each query is sent once per repeat and compared once.
    assert queries == 2 * report["variation"]["queries"]["pairs"] > 0
    assert all(isinstance(i, list) for i in inputs if not isinstance(i, str))

    first = len(answered)
    assert main_cli(_argv(tmp_path, "run2"), env) == 0
    again = json.loads((tmp_path / "run2" / "run.json").read_text(encoding="utf-8"))
    assert len(answered) == first and again["requests"] == 0
    assert again["cache_misses"] == 0 and again["cache_hits"] == report["cache_misses"]

    out = capsys.readouterr()
    for text in (out.out, out.err, json.dumps(report)):
        assert _KEY not in text
    assert _KEY.encode() not in (tmp_path / "cache" / "vectors.sqlite").read_bytes()


@requires_ocr
def test_request_cap_stops_the_run_as_inconclusive(tmp_path, provider, capsys):
    url, answered = provider
    env = {"EMBED_BASE_URL": url, "EMBED_MODEL": embed_server.MODEL}
    code = main_cli(_argv(tmp_path, "run", "--max-requests", "3"), env)
    assert code == EXIT_INCONCLUSIVE
    # The cap's request count is sent, and not one more.
    assert len(answered) == 3
    err = capsys.readouterr().err
    assert "INCONCLUSIVE" in err and "3 requests" in err
    assert not (tmp_path / "run" / "run.json").exists()
