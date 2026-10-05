"""Answer-evaluation cases against the built synthetic index (#604).

Runs in ``make baseline`` (and the CI baseline job) after
``tests.baseline.build`` was given the case file, so every case question
has a query vector. Two layers:

1. References. Every expected fact's excerpt is in the indexed text of
   each message it cites, every evidence ref names an indexed message or
   thread, and every unanswerable case's golden question exists. A case
   cannot rest on a fact the corpus does not hold, and the references
   are checked against the corpus, not against what retrieval returns.
2. The harness end to end. Every case runs through the real
   ``ask_mailbox`` handler with a scripted answerer (no network) that
   writes the case's expected values citing the supplied passages, and a
   scripted judge. Each run must complete, its captured evidence must
   match the prompt the model received, a case whose evidence was all
   supplied must pass every deterministic check, and the prompt-budget
   case must show its evidence omitted by prompt assembly.
"""

import asyncio
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlite_vec
from src.lib.inference import PromptBudget
from src.lib.sqlite import Database

from tests.answer_eval import __main__ as cli
from tests.answer_eval.cases import DIMENSIONS, Case, load_cases, message_id_of, thread_id_of
from tests.answer_eval.config import LayerConfig
from tests.answer_eval.graders import _fold, _mentions
from tests.answer_eval.harness import evaluate
from tests.answer_eval.runner import (
    NonSyntheticIndexError,
    PrecomputedEmbedder,
    RunContext,
    corpus_manifest,
    index_identity,
)

pytestmark = pytest.mark.baseline

CASES = load_cases()
GOLDEN = json.loads((Path(__file__).parent / "golden.json").read_text(encoding="utf-8"))
_LABEL = re.compile(r"\[(E\d+) \| message ([^ |]+)")


@pytest.fixture(scope="module")
def baseline_dir() -> Path:
    baseline_dir = os.environ.get("BASELINE_DIR")
    if not baseline_dir:
        pytest.skip("BASELINE_DIR not set; run `make baseline`")
    return Path(baseline_dir)


@pytest.fixture(scope="module")
def baseline_db(baseline_dir: Path) -> Database:
    return Database(str(baseline_dir / "mail.db"))


@pytest.fixture(scope="module")
def indexed_text(baseline_db: Database) -> dict[str, str]:
    """Each indexed message's chunk text (body and attachments), by Message-ID."""
    with closing(baseline_db._connect()) as conn:
        rows = conn.execute(
            "SELECT m.message_id, c.text FROM message_chunks c "
            "JOIN messages m ON m.claimant_id = c.claimant_id ORDER BY c.chunk_index"
        ).fetchall()
    texts: dict[str, list[str]] = {}
    for message_id, text in rows:
        texts.setdefault(message_id, []).append(text)
    return {m: " ".join(" ".join(parts).split()) for m, parts in texts.items()}


def _ref_message(ref: str) -> str:
    return message_id_of(ref) or thread_id_of(ref)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_case_references_resolve(case: Case, indexed_text: dict[str, str]) -> None:
    for fact in case.expected_facts:
        excerpt = " ".join(fact.excerpt.split())
        assert any(excerpt in indexed_text.get(_ref_message(r), "") for r in fact.sources), (
            f"{case.id} {fact.id}: excerpt not in the indexed text of {fact.sources}"
        )
    for group in case.required_evidence:
        for ref in group:
            assert _ref_message(ref) in indexed_text, f"{case.id}: {ref} is not indexed"
    if case.golden_unanswerable:
        assert case.golden_unanswerable in {u["id"] for u in GOLDEN["unanswerable"]}


def test_index_is_recognized_as_synthetic(baseline_db: Database) -> None:
    identity = index_identity(baseline_db)
    assert identity["corpus"] == "synthetic-baseline"
    assert identity["messages"] == len(corpus_manifest())


@pytest.mark.parametrize(
    "tamper",
    [
        # Private text under a copied baseline claimant ID.
        "UPDATE message_chunks SET text = text || ' privatemarker' WHERE rowid = "
        "(SELECT MIN(rowid) FROM message_chunks)",
        "UPDATE threads SET body_text = body_text || ' privatemarker' WHERE rowid = "
        "(SELECT MIN(rowid) FROM threads)",
        "UPDATE threads SET subject = 'privatemarker' WHERE rowid = (SELECT MIN(rowid) FROM threads)",
        # Review round 2: every other field a prompt carries.
        "UPDATE threads SET display_subject = 'privatemarker' WHERE rowid = "
        "(SELECT MIN(rowid) FROM threads)",
        "UPDATE message_participants SET name = 'privatemarker' WHERE rowid = "
        "(SELECT MIN(rowid) FROM message_participants)",
        "UPDATE attachments SET filename = 'privatemarker.pdf' WHERE rowid = "
        "(SELECT MIN(rowid) FROM attachments)",
        "UPDATE messages SET subject = 'privatemarker' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages)",
        # A message the committed corpus does not have.
        "UPDATE messages SET claimant_id = claimant_id || 'x' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages)",
        "DELETE FROM messages WHERE rowid = (SELECT MIN(rowid) FROM messages)",
    ],
)
def test_tampered_index_is_refused(baseline_dir: Path, tmp_path: Path, tamper: str) -> None:
    """Review round 1: baseline-looking IDs alone are not enough."""
    copy = tmp_path / "mail.db"
    shutil.copy(baseline_dir / "mail.db", copy)
    conn = sqlite3.connect(copy)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(tamper)
    conn.commit()
    conn.close()
    with pytest.raises(NonSyntheticIndexError):
        index_identity(Database(str(copy)))


class _OracleAnswerer:
    """Writes each case's expected values, citing the passages that hold them.

    It reads the labelled headers of the prompt it receives, as a model
    would, so it can only cite what prompt assembly actually supplied.
    """

    mode = "anthropic"
    base_url = ""

    def __init__(self, case: Case) -> None:
        self.case = case

    async def complete(self, system: str, user: str) -> str:
        if not self.case.answerable:
            return "Not found in the provided emails: nothing in them answers this."
        labels = dict(_LABEL.findall(user))  # label -> claimant ID
        blocks = re.split(r"(?=\[E\d+ \|)", user)
        # Each passage's text without its header, whose message ID and
        # date would otherwise match short values such as "4".
        passages = {
            b.split(" |", 1)[0][1:]: _fold(b.split("]", 1)[1]) for b in blocks if b.startswith("[E")
        }
        sentences = []
        for group in self.case.required_evidence:
            wanted = {_ref_message(r) for r in group}
            hit = next((lbl for lbl, c in labels.items() if c.split("#")[0] in wanted), None)
            if hit:
                sentences.append(f"This passage bears on the question [{hit}].")
        for group in self.case.must_include:
            value = group[0]
            hit = next((lbl for lbl, text in passages.items() if _mentions(text, value)), None)
            mark = f"[{hit}]" if hit else "[unsupported]"
            sentences.append(f"The value is {value} {mark}.")
        if not any("[E" in s for s in sentences):
            return "Not found in the provided emails: no passage holds the answer."
        return " ".join(sentences)


class _StubJudge:
    """Approves one claim per numbered statement that cites a passage,
    naming that statement and its labels; never touches the network."""

    def __init__(self) -> None:
        self.case: Case | None = None

    async def complete(self, system: str, user: str) -> str:
        case = self.case
        assert case is not None
        statements = re.findall(r'<untrusted_answer statement="(\d+)">\n(.*?)\n</untrusted', user)
        claims = [
            {
                "claim": "c",
                "statement": int(index),
                "cited": sorted(set(re.findall(r"\[(E\d+)\]", text))),
                "verdict": "supported",
                "explanation": "ok",
            }
            for index, text in statements
            if "[E" in text
        ]
        return json.dumps(
            {
                "claims": claims,
                "facts": [{"id": f.id, "covered": True} for f in case.expected_facts],
                "prohibited": [
                    {"index": i, "asserted": False} for i in range(1, len(case.must_not_assert) + 1)
                ],
                "dimensions": {
                    d: {"result": "pass" if case.criteria[d] else "not_applicable"}
                    for d in DIMENSIONS
                },
            }
        )


def _judge_config() -> LayerConfig:
    return LayerConfig(
        layer="JUDGE",
        mode="openai",
        base_url="http://127.0.0.1:9",
        model="stub-judge",
        api_key="unused",  # pragma: allowlist secret
        timeout_secs=30.0,
        max_tokens=2048,
        context_tokens=0,
        max_input_chars=60_000,
    )


def _evaluate_all(baseline_dir: Path, baseline_db: Database) -> list[dict]:
    vectors = json.loads((baseline_dir / "query_vectors.json").read_text(encoding="utf-8"))
    records = []
    judge = _StubJudge()
    for case in CASES:
        ctx = RunContext(
            db=baseline_db,
            embed_client=PrecomputedEmbedder(vectors),
            inference_client=_OracleAnswerer(case),
            prompt_budget=PromptBudget(),
            expected_embed_dim=baseline_db.get_embedding_dim(),
        )
        judge.case = case
        rows, _ = asyncio.run(
            evaluate([case], ctx, judge_client=judge, judge_config=_judge_config())
        )
        records += rows
    return records


@pytest.fixture(scope="module")
def records(baseline_dir: Path, baseline_db: Database) -> dict[str, dict]:
    return {r["id"]: r for r in _evaluate_all(baseline_dir, baseline_db)}


def test_every_case_completes_and_is_judged(records: dict[str, dict]) -> None:
    assert set(records) == {c.id for c in CASES}
    for cid, r in records.items():
        assert r["status"] == "ok", cid
        assert r["judge"]["status"] == "ok", (cid, r["judge"]["error"])
        assert r["deterministic"]["checks"]["prompt_matches_capture"] == "pass", cid


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_supplied_evidence_lets_a_correct_answer_pass(case: Case, records: dict[str, dict]) -> None:
    r = records[case.id]
    det = r["deterministic"]
    if det["prompt_coverage"] in (None, 1.0):
        assert det["passed"], (case.id, det["checks"])
        assert r["attribution"] == []
    else:
        # Evidence never reached the model: the failure must be pinned on
        # the stage that lost it, not on synthesis.
        assert not det["passed"]
        assert {"retrieval", "prompt_assembly"} & set(r["attribution"]), r["attribution"]


def test_cases_missing_evidence_are_the_known_ones(records: dict[str, dict]) -> None:
    """Which cases lose evidence before the model sees it.

    The hashed embedder has no semantics, so one natural-language
    question misses its thread; the prompt-budget case loses one source
    to its budget by design. A change here is a retrieval or prompt
    assembly change: explain it in the PR and update the sets.
    """
    lost = {
        cid: sorted(set(r["attribution"]) & {"retrieval", "prompt_assembly"})
        for cid, r in records.items()
        if r["deterministic"]["prompt_coverage"] not in (None, 1.0)
    }
    assert lost == {
        "ask-lisbon-dates": ["retrieval"],
        "ask-kayak-tight-budget": ["prompt_assembly"],
    }


def test_prompt_budget_case_detects_omitted_evidence(records: dict[str, dict]) -> None:
    budget = [c for c in CASES if c.category == "prompt_budget"]
    assert budget
    for case in budget:
        r = records[case.id]
        det = r["deterministic"]
        assert det["retrieval_recall"] == 1.0, det
        assert det["prompt_coverage"] < 1.0, det
        assert det["checks"]["required_evidence_cited"] == "fail"
        assert "prompt_assembly" in r["attribution"]


def test_cli_run_writes_report(
    baseline_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The ``run`` command end to end, with both providers scripted."""
    case = next(c for c in CASES if c.id == "ask-roof-total")
    judge = _StubJudge()
    judge.case = case

    def client(self: LayerConfig):
        return _OracleAnswerer(case) if self.layer == "INFERENCE" else judge

    monkeypatch.setattr(LayerConfig, "client", client)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in ("inference_api_key.txt", "judge_api_key.txt"):
        (secrets / name).write_text("unauthenticated\n")
        (secrets / name).chmod(0o600)
    for var, value in {
        "INFERENCE_MODE": "openai",
        "INFERENCE_BASE_URL": "http://127.0.0.1:9/v1",
        "INFERENCE_MODEL": "stub-answerer",
        "JUDGE_MODE": "openai",
        "JUDGE_BASE_URL": "http://127.0.0.1:9/v1",
        "JUDGE_MODEL": "stub-judge",
    }.items():
        monkeypatch.setenv(var, value)
    out = tmp_path / "run.json"
    code = cli.main(
        [
            "run",
            "--index-dir",
            str(baseline_dir),
            "--out",
            str(out),
            "--secrets-dir",
            str(secrets),
            "--case",
            case.id,
        ]
    )
    assert code == cli.EXIT_OK
    report = json.loads(out.read_text())
    assert report["counts"]["completed"] == 1
    assert report["identity"]["judge"]["endpoint"] == "host-local"
    assert out.stat().st_mode & 0o777 == 0o600
    assert "unauthenticated" not in out.read_text()
    assert cli.main(["compare", str(out), str(out)]) == cli.EXIT_OK
