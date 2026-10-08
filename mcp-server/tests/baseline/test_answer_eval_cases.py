"""Answer-evaluation cases against the built synthetic index (#604).

Runs in ``make baseline`` (and the CI baseline job) after
``tests.baseline.build`` was given the case file, so every case question
has a query vector. Two layers:

1. References. Every expected fact's excerpt is in the indexed text of
   each message it cites, every evidence ref names an indexed message or
   thread, and every unanswerable case's golden question exists. A case
   cannot rest on a fact the corpus does not hold, and the references
   are checked against the corpus, not against what retrieval returns.
2. The harness end to end. Every case runs through the real handler
   of its tool (``ask_mailbox`` or ``summarize_thread``, #656, or an
   experimental tool, #1240; ``extract_from_emails`` cases are checked
   in layer 1 only until #1287) with a
   scripted answerer (no network) that writes the case's expected
   values citing the supplied passages, and a scripted judge. Each run
   must complete, its captured evidence must match the prompt the model
   received, a case whose evidence was all
   supplied must pass every deterministic check, and each prompt-budget
   case must show its evidence omitted by prompt assembly and disclosed
   by the server's coverage note.
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
# Layer 2 leaves out ``extract_from_emails``: the scripted answerer
# cannot yet return one record per expected item (#1287).
HARNESS_CASES = [c for c in CASES if c.tool != "extract_from_emails"]
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
        # #1140: a display name past the first.
        "INSERT INTO message_participant_names SELECT claimant_id, role, address, "
        "'privatemarker' FROM message_participants WHERE rowid = "
        "(SELECT MIN(rowid) FROM message_participants)",
        "UPDATE attachments SET filename = 'privatemarker.pdf' WHERE rowid = "
        "(SELECT MIN(rowid) FROM attachments)",
        "UPDATE messages SET subject = 'privatemarker' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages)",
        # #671: dates reach the prompt through the labelled chunk header.
        "UPDATE messages SET sent_at = 'privatemarker' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages)",
        "UPDATE messages SET sent_at = '2031-01-01T00:00:00+00:00' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages)",
        "UPDATE messages SET occurred_at = '2031-01-01T00:00:00+00:00' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages)",
        # #674: the stored Message-ID is each passage's origin in the judge prompt.
        "UPDATE messages SET message_id = 'privatemarker@private.example' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages)",
        # A message the committed corpus does not have.
        "UPDATE messages SET claimant_id = claimant_id || 'x' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages)",
        "DELETE FROM messages WHERE rowid = (SELECT MIN(rowid) FROM messages)",
        # #675: a consistent index whose claimant suffixes are 8 hex
        # digits, not the indexer's 16.
        "".join(
            f"UPDATE {table} SET claimant_id = substr(claimant_id, 1, instr(claimant_id, '#') + 8);"
            for table in (
                "messages",
                "message_thread_map",
                "message_participants",
                "message_chunks",
                "attachments",
            )
        ),
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
    conn.executescript(tamper)
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
        self.unsupported: list[list[str]] = []

    async def complete(self, system: str, user: str) -> str:
        """Prose for ``ask_mailbox`` and ``summarize_thread``; for the
        experimental tools (#1240) the same sentences as the JSON reply
        their prompts ask for, each sentence one entry citing its label."""
        text = self._prose(user)
        if self.case.tool not in ("brief_issue", "check_conclusion"):
            return text
        abstained = text.startswith("Not found")
        entries = [
            (s.split(" [")[0], re.findall(r"\[(E\d+)\]", s))
            for s in ([] if abstained else re.split(r"(?<=\.) ", text))
        ]
        if self.case.tool == "brief_issue":
            decisions = [{"decision": t, "labels": labels} for t, labels in entries]
            return json.dumps(
                {
                    "chronology": [],
                    "positions": [],
                    "decisions": decisions,
                    "open_questions": [],
                    "conflicts": [],
                    "insufficient_evidence": abstained,
                }
            )
        findings = [
            {"relation": "supports", "explanation": t, "labels": labels} for t, labels in entries
        ]
        verdict = "The passages do not address it." if abstained else "Supported."
        return json.dumps(
            {"verdict_summary": verdict, "findings": findings, "insufficient_evidence": abstained}
        )

    def _prose(self, user: str) -> str:
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
            # The first alternative some passage states, so the group's
            # order (e.g. "4 chaperones" before the source's "chaperone
            # volunteers needed: 4") does not decide whether it is found.
            found = (
                (value, lbl)
                for value in group
                for lbl, text in passages.items()
                if _mentions(text, value)
            )
            value, hit = next(found, (group[0], None))
            if hit is None:
                self.unsupported.append(group)
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


# Value groups the scripted answerer found in no passage, per case.
_ORACLE_MISSES: dict[str, list[list[str]]] = {}


# Details (passages supplied) per case, kept by ``_evaluate_all``.
_DETAILS: dict[str, dict] = {}


def _evaluate_all(baseline_dir: Path, baseline_db: Database) -> list[dict]:
    vectors = json.loads((baseline_dir / "query_vectors.json").read_text(encoding="utf-8"))
    records = []
    judge = _StubJudge()
    for case in HARNESS_CASES:
        oracle = _OracleAnswerer(case)
        _ORACLE_MISSES[case.id] = oracle.unsupported
        ctx = RunContext(
            db=baseline_db,
            embed_client=PrecomputedEmbedder(vectors),
            inference_client=oracle,
            prompt_budget=PromptBudget(),
            expected_embed_dim=baseline_db.get_embedding_dim(),
        )
        judge.case = case
        rows, details = asyncio.run(
            evaluate([case], ctx, judge_client=judge, judge_config=_judge_config())
        )
        records += rows
        _DETAILS[case.id] = details[0]
    return records


@pytest.fixture(scope="module")
def records(baseline_dir: Path, baseline_db: Database) -> dict[str, dict]:
    return {r["id"]: r for r in _evaluate_all(baseline_dir, baseline_db)}


def test_every_case_completes_and_is_judged(records: dict[str, dict]) -> None:
    assert set(records) == {c.id for c in HARNESS_CASES}
    for cid, r in records.items():
        assert r["status"] == "ok", cid
        assert r["judge"]["status"] == "ok", (cid, r["judge"]["error"])
        assert r["deterministic"]["checks"]["prompt_matches_capture"] == "pass", cid
        assert r["tool"] == next(c.tool for c in CASES if c.id == cid), cid


def test_experimental_smoke_cases_pass_end_to_end(records: dict[str, dict]) -> None:
    """#1240: each experimental tool's smoke cases run through the real
    handler, pass every deterministic check on the first reply, and the
    abstention cases abstain through the tool's own flag."""
    smoke = [c for c in CASES if c.tool in ("brief_issue", "check_conclusion")]
    assert {c.tool for c in smoke} == {"brief_issue", "check_conclusion"}
    for case in smoke:
        r = records[case.id]
        assert r["deterministic"]["passed"], (case.id, r["deterministic"]["checks"])
        assert r["repair_attempted"] is False, case.id
        assert r["deterministic"]["abstained"] is (not case.answerable), case.id
        if case.answerable:
            assert r["deterministic"]["citation_coverage"] == 1.0, case.id


@pytest.mark.parametrize("case", HARNESS_CASES, ids=lambda c: c.id)
def test_supplied_evidence_lets_a_correct_answer_pass(case: Case, records: dict[str, dict]) -> None:
    r = records[case.id]
    det = r["deterministic"]
    if det["prompt_coverage"] in (None, 1.0) or case.expected_handling == "disclose_missing":
        # #820: an omission the server's coverage note disclosed is what
        # a disclose_missing case expects.
        assert det["passed"], (case.id, det["checks"])
        assert r["attribution"] == []
    else:
        # Evidence never reached the model: the failure must be pinned on
        # the stage that lost it, not on synthesis.
        assert not det["passed"]
        assert {"retrieval", "prompt_assembly"} & set(r["attribution"]), r["attribution"]


def test_answerer_finds_every_value_where_evidence_arrived(records: dict[str, dict]) -> None:
    """Codex round 2 on #763: whenever the evidence reached the prompt,
    the scripted answerer must cite a passage for every ``must_include``
    group a source states; an ``[unsupported]`` value would let the
    baseline pass without showing the value is in cited evidence. A
    group is stated when one of its values is in a fact excerpt (quoted
    from its source, checked above); a computed value such as a total is
    not, and no passage can hold it."""
    by_id = {c.id: c for c in CASES}
    for cid, r in records.items():
        if r["deterministic"]["prompt_coverage"] not in (None, 1.0):
            continue
        excerpts = [_fold(f.excerpt) for f in by_id[cid].expected_facts]
        stated = [
            g for g in _ORACLE_MISSES[cid] if any(_mentions(e, v) for e in excerpts for v in g)
        ]
        assert stated == [], cid


# Each #755 decoy case's out-of-scope sibling message (review round 6).
_DECOYS = {
    "ask-walker-rate-sender": "t75.2",
    "ask-swim-practice-date": "t76.2",
    "ask-swim-scope-stated": "t76.2",
    "ask-garden-plot-trash": "t77.2",
}


@pytest.mark.parametrize("case_id", sorted(_DECOYS))
def test_decoy_reaches_the_answering_model(case_id: str, records: dict[str, dict]) -> None:
    """Review round 6: a decoy case tests nothing unless the out-of-scope
    sibling's passage is in the prompt the model received."""
    supplied = {p["message_id"] for p in _DETAILS[case_id]["passages"].values()}
    assert message_id_of(_DECOYS[case_id]) in supplied, (case_id, sorted(supplied))


# Each #910 attachment-layer case's shape, as the (message, source)
# passages it rests on: every passage holding an expected fact, plus
# the decoy. The body and the attachment that disagree; the replaced
# sheet, its revision and the body line saying it replaces the first;
# the in-scope body and the Trash reply's stale attachment.
_ATTACHMENT_SHAPES = {
    "ask-wall-bill-attachment": {("t93.1", "body"), ("t93.1", "attachment")},
    "ask-armchair-revised": {
        ("t94.1", "attachment"),
        ("t94.2", "attachment"),
        ("t94.2", "body"),
    },
    "ask-lido-locker-trash": {("t95.1", "body"), ("t95.2", "attachment")},
}


@pytest.mark.parametrize("case_id", sorted(_ATTACHMENT_SHAPES))
def test_attachment_shape_reaches_the_answering_model(
    case_id: str, records: dict[str, dict]
) -> None:
    """#910: an attachment-layer case tests nothing unless every passage
    of its shape is in the prompt the model received. ``required_evidence``
    names messages, so the grader counts any passage of a message as
    its evidence (#1182); this test is what pins the passages. Each must
    reach the prompt whole: a passage cut to the budget may have lost the
    sentence or value the shape rests on (Codex round 2 on #1177)."""
    supplied = _whole_passages(case_id)
    wanted = {(message_id_of(ref), source) for ref, source in _ATTACHMENT_SHAPES[case_id]}
    assert wanted <= supplied, (case_id, sorted(wanted - supplied))


# Each #911 body-only case's shape, as for #910 above: every passage
# holding an expected fact, plus the decoy. The negated value, the real
# figure and the separate job billed at the negated value; the one
# notice giving both rates and the effective date (each question); the
# first order, the revision replacing count and price together, and the
# later message repeating the old pair.
_BODY_SHAPES = {
    "ask-conservatory-real-price": {("t96.1", "body"), ("t97.1", "body"), ("t97.2", "body")},
    "ask-darkroom-rate-2027": {("t98.1", "body")},
    "ask-darkroom-from-2028": {("t98.1", "body")},
    "ask-trestles-revised": {("t99.1", "body"), ("t99.2", "body"), ("t99.3", "body")},
}


@pytest.mark.parametrize("case_id", sorted(_BODY_SHAPES))
def test_body_shape_reaches_the_answering_model(case_id: str, records: dict[str, dict]) -> None:
    """#911: a negated-distractor, effective-date or paired-correction
    case tests nothing unless every passage of its shape reaches the
    prompt whole: the decoy (the separate job's bill, the later message
    repeating the old pair) must be live, and a passage cut to the
    budget may have lost the value or the date the case rests on."""
    supplied = _whole_passages(case_id)
    wanted = {(message_id_of(ref), source) for ref, source in _BODY_SHAPES[case_id]}
    assert wanted <= supplied, (case_id, sorted(wanted - supplied))


# Each #975 late-disposition case's shape: the passage holding its
# expected facts plus its decoy. The outcome question needs the closing
# message (t100.11) and has the other site's undisputed statement as its
# decoy; the first-explanation question needs the early explanation
# (t100.3) and has the closing message, which contradicts it, as its
# decoy, so a recency-only answer fails once both are shown.
_LATE_DISPOSITION_SHAPES = {
    "ask-dispenser-hire-outcome": {("t100.11", "body"), ("t101.1", "body")},
    "ask-dispenser-first-explanation": {("t100.3", "body"), ("t100.11", "body")},
}
# Known gap (#974, #858): each thread's passages are chosen by vector
# distance to the question alone, and t100.11 shares no word with
# either question, so six closer t100 passages take its place. A fix
# makes the strict xfail below pass and must move these cases out of
# the known gap.
_LATE_DISPOSITION_GAP = ("t100.11", "body")


@pytest.mark.xfail(
    strict=True,
    reason="#974: the late closing message t100.11 is not selected for the prompt",
)
@pytest.mark.parametrize("case_id", sorted(_LATE_DISPOSITION_SHAPES))
def test_late_disposition_shape_reaches_the_answering_model(
    case_id: str, records: dict[str, dict]
) -> None:
    """#975: a late-disposition case tests the #974 gap only when every
    passage of its shape reaches the prompt whole."""
    supplied = _whole_passages(case_id)
    wanted = {(message_id_of(ref), source) for ref, source in _LATE_DISPOSITION_SHAPES[case_id]}
    assert wanted <= supplied, (case_id, sorted(wanted - supplied))


@pytest.mark.parametrize("case_id", sorted(_LATE_DISPOSITION_SHAPES))
def test_late_disposition_shape_short_of_the_known_gap(
    case_id: str, records: dict[str, dict]
) -> None:
    """#975: until #974 is fixed, every other passage of the shape still
    reaches the prompt whole (the decoy is live, the early explanation
    is shown), and the closing message is the one passage missing."""
    supplied = _whole_passages(case_id)
    wanted = {(message_id_of(ref), source) for ref, source in _LATE_DISPOSITION_SHAPES[case_id]}
    gap = (message_id_of(_LATE_DISPOSITION_GAP[0]), _LATE_DISPOSITION_GAP[1])
    assert wanted - supplied == {gap}, (case_id, sorted(wanted - supplied))


def _whole_passages(case_id: str) -> set[tuple[str | None, str]]:
    """The (message, source) pairs of the passages a case's prompt
    carried uncut."""
    return {
        (p["message_id"], p["source"])
        for p in _DETAILS[case_id]["passages"].values()
        if not p["truncated"]
    }


def test_cases_missing_evidence_are_the_known_ones(records: dict[str, dict]) -> None:
    """Which cases lose evidence before the model sees it.

    The hashed embedder has no semantics, so one natural-language
    question misses its thread; each prompt-budget case loses one source
    to its budget by design (``ask-kayak-tight-budget`` a thread,
    ``summarize-hall-open-points`` the message past the window, #656).
    ``ask-dispenser-hire-outcome`` (#975) is the known #974 gap: its
    thread is retrieved, but per-thread passage selection leaves out the
    late closing message, which shares no word with the question. A
    change here is a retrieval or prompt assembly change: explain it in
    the PR and update the sets.
    """

    def stages(det: dict) -> list[str]:
        lost_at = []
        if det["retrieval_recall"] < 1.0:
            lost_at.append("retrieval")
        if det["prompt_coverage"] < det["retrieval_recall"]:
            lost_at.append("prompt_assembly")
        return lost_at

    lost = {
        cid: stages(r["deterministic"])
        for cid, r in records.items()
        if r["deterministic"]["prompt_coverage"] not in (None, 1.0)
    }
    assert lost == {
        "ask-lisbon-dates": ["retrieval"],
        "ask-kayak-tight-budget": ["prompt_assembly"],
        "summarize-hall-open-points": ["prompt_assembly"],
        "ask-dispenser-hire-outcome": ["prompt_assembly"],
    }


def test_prompt_budget_case_detects_omitted_evidence(records: dict[str, dict]) -> None:
    budget = [c for c in CASES if c.category == "prompt_budget"]
    assert {c.tool for c in budget} == {"ask_mailbox", "summarize_thread"}
    # Codex round 2 on #656's PR: the summary's thread text (E1) the
    # window cut short is captured as truncated; the newest message
    # shown whole is not.
    passages = _DETAILS["summarize-hall-open-points"]["passages"]
    assert passages["E1"]["truncated"] is True, passages["E1"]
    assert all(not p["truncated"] for label, p in passages.items() if label != "E1"), passages
    for case in budget:
        r = records[case.id]
        det = r["deterministic"]
        assert det["retrieval_recall"] == 1.0, det
        assert det["prompt_coverage"] < 1.0, det
        assert det["checks"]["omission_disclosed"] == "pass", det


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
