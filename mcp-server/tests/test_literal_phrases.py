"""Literal phrases use original character boundaries, never token positions."""

import hashlib
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from src.lib.literal import candidate_tokens, contains, normalize_phrase
from src.lib.predicates import InvalidFilterError, Where, normalize_where

from tests.conftest import _insert_message, claimant_of

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from indexer.src import literal as writer  # noqa: E402 -- test-only cross-service writer contract


@pytest.mark.parametrize(
    "text,needle,match",
    [
        ("Straße", "STRASSE", True),
        ("Straße", "sse", True),
        ("ß", "s", False),
        ("ß", "ss", True),
        ("İ", "i", False),
        ("İ", "i\u0307", True),
        ("before\x00after", "e\x00a", True),
        ("A  B", "a b", False),
        ("A-B", "a b", False),
        ("猫犬鳥", "犬鳥", True),
        ("astral \U0001f600 end", "\U0001f600", True),
    ],
)
def test_aligned_casefold(text, needle, match):
    assert contains(text, needle.casefold(), [(0, len(text))]) is match


def test_classified_phrase_cannot_cross_run_or_source_boundary():
    assert not contains("body quote", "y q", [(0, 4), (5, 10)])
    assert contains("body quote", "quote", [(5, 10)])


def test_operand_preserves_whitespace_and_limits_after_casefold():
    assert normalize_phrase(" A ") == " a "
    assert normalize_phrase(" " * 1000) == " " * 1000
    for invalid in ("", "ß" * 501, "\ud800"):
        with pytest.raises(ValueError):
            normalize_phrase(invalid)


def test_candidate_shape_is_fixed_nonpositional_distinct_and_nul_safe():
    grams = candidate_tokens(("aa\x00ß猫" * 200).casefold())
    assert 1 <= len(grams) <= 8
    assert len(set(grams)) == len(grams)
    assert all(gram.isascii() and gram.isalnum() and "\x00" not in gram for gram in grams)
    assert len(candidate_tokens("a" * 1000)) == 1


def expression(name, value, negate=False):
    return normalize_where(
        Where.model_validate({"all": [{"leaf": name, "value": value, "negate": negate}]})
    )


def add_source(conn, mid, texts, complete=True):
    cid = claimant_of(mid)
    generation = hashlib.sha256(repr(texts).encode()).hexdigest()
    conn.execute(
        "UPDATE messages SET full_body_complete=?,full_body_version=1,full_body_generation=? WHERE claimant_id=?",
        (int(complete), generation, cid),
    )
    for part, (text, kind) in enumerate(texts):
        conn.execute(
            "INSERT INTO message_body_parts VALUES (?,?, -1,'plain',1,0,?,?,?,?)",
            (cid, part, len(text), len(text.encode()), generation, len(text)),
        )
        for ordinal, start in enumerate(range(0, len(text), 8192)):
            piece = text[start : start + 8192]
            conn.execute(
                "INSERT INTO message_body_spans VALUES(?,?,?,?,?,?)",
                (cid, part, ordinal, start, start + len(piece), piece.encode()),
            )
        if text:
            conn.execute(
                "INSERT INTO message_body_ranges VALUES(?,?,?,?,?)", (cid, part, 0, len(text), kind)
            )
    writer.rebuild_body(conn.cursor(), cid)
    return cid


@pytest.fixture
def phrase_db(empty_db):
    with closing(sqlite3.connect(empty_db.path)) as conn:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        for mid in ("one", "two", "three", "four"):
            _insert_message(
                conn,
                message_id=mid,
                thread_id="t-" + mid,
                body="synthetic body",
                sent_at="2024-01-01T00:00:00+00:00",
            )
        add_source(
            conn, "one", [("x" * 8190 + "Straße\x00cat", "quote"), ("signature words", "signature")]
        )
        add_source(conn, "two", [("unrelated synthetic words", "body")], complete=False)
        add_source(conn, "three", [("STRASSE\x00CAT", "body")])
        add_source(conn, "four", [("ab", "body"), ("cd", "body")])
        conn.commit()
    return empty_db


def test_real_reader_crosses_fragments_but_not_mime_sources(phrase_db):
    page = phrase_db.query_messages(where=expression("body_phrase", "STRASSE\x00cat"))
    assert (page.total_matches, page.indeterminate) == (2, 1)
    assert phrase_db.query_messages(where=expression("body_phrase", "bc")).total_matches == 0
    assert phrase_db.query_messages(where=expression("quote_phrase", "Straße")).total_matches == 1
    assert (
        phrase_db.query_messages(where=expression("authored_body_phrase", "Straße")).total_matches
        == 1
    )
    assert (
        phrase_db.query_messages(
            where=expression("signature_phrase", "signature words")
        ).total_matches
        == 1
    )
    assert (
        phrase_db.query_messages(
            where=expression("forwarded_phrase", "signature words")
        ).total_matches
        == 0
    )


@pytest.mark.parametrize("reader", ["query_messages", "query_threads", "aggregate_messages"])
def test_every_reader_uses_one_connection_and_reuses_exact_verification(
    phrase_db, monkeypatch, reader
):
    import src.lib.literal as literal

    opens = []
    connect = phrase_db._connect

    def counted():
        conn = connect()
        opens.append(conn)
        return conn

    checks = []
    original = literal.contains

    def checked(*args):
        checks.append(1)
        return original(*args)

    monkeypatch.setattr(phrase_db, "_connect", counted)
    monkeypatch.setattr(literal, "contains", checked)
    args = {"where": expression("body_phrase", "strasse")}
    if reader == "aggregate_messages":
        args["group_by"] = "folder"
    page = getattr(phrase_db, reader)(**args)
    matches = page.total_matching_messages if reader == "query_threads" else page.total_matches
    assert matches == 2
    assert len(opens) == 1
    assert len(checks) == 2


def test_three_valued_or_and_not_and_cursor_binding(phrase_db):
    where = normalize_where(
        Where.model_validate(
            {
                "all": [
                    {
                        "any": [
                            {"leaf": "body_phrase", "value": "absent", "negate": True},
                            {"leaf": "body_phrase", "value": "strasse"},
                        ]
                    }
                ]
            }
        )
    )
    page = phrase_db.query_messages(where=where, limit=1)
    assert (page.total_matches, page.indeterminate) == (3, 1)
    assert page.next_cursor
    assert phrase_db.query_messages(where=where, limit=1, cursor=page.next_cursor).offset == 1
    with pytest.raises(InvalidFilterError):
        phrase_db.query_messages(
            where=expression("body_phrase", "different"), cursor=page.next_cursor
        )


def test_preflight_refuses_before_source_transfer(phrase_db, monkeypatch):
    import src.lib.literal as literal

    monkeypatch.setattr(literal, "MAX_LEAF_CANDIDATES", 1)
    monkeypatch.setattr(literal, "contains", lambda *a: pytest.fail("source verification ran"))
    with pytest.raises(InvalidFilterError, match="work limit exhausted"):
        phrase_db.query_messages(where=expression("body_phrase", "strasse"))


def test_oversized_source_is_withheld_before_decode(phrase_db, monkeypatch):
    with closing(sqlite3.connect(phrase_db.path)) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE message_body_spans SET data=? WHERE claimant_id=?",
            (b"SYNTHETIC_PRIVATE_MARKER" * 100000, claimant_of("three")),
        )
        conn.commit()
    transferred = []
    connect = phrase_db._connect

    def measured():
        conn = connect()

        def row(cursor, values):
            transferred.append(sum(len(x) for x in values if isinstance(x, bytes)))
            return sqlite3.Row(cursor, values)

        conn.row_factory = row
        return conn

    monkeypatch.setattr(phrase_db, "_connect", measured)
    with pytest.raises(InvalidFilterError, match="invalid fragments") as error:
        phrase_db.query_messages(where=expression("body_phrase", "strasse"))
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(error.value)
    assert max(transferred) < 65536


@pytest.mark.parametrize("stamp", ["pdf-digital@5", "pdf-ocr@5"])
def test_attachment_generation_defer_and_incomplete_evidence(phrase_db, stamp):
    from tests.conftest import _insert_attachment, _insert_extraction

    with closing(sqlite3.connect(phrase_db.path)) as conn:
        _insert_attachment(
            conn,
            attachment_id="payload",
            message_id="three",
            thread_id="t-three",
            filename="synthetic.pdf",
        )
        _insert_extraction(
            conn,
            attachment_id="payload",
            extracted_text="alpha " * 1400 + "NUL\x00Straße",
            extractor=stamp,
        )
        conn.execute("UPDATE attachments SET text_complete=1,text_extractor=?", (stamp,))
        conn.execute(
            "UPDATE messages SET has_attachments=1,attachment_rows_current=1 WHERE claimant_id=?",
            (claimant_of("three"),),
        )
        writer.rebuild_attachments(conn.cursor(), claimant_of("three"), True)
        conn.commit()
    assert (
        phrase_db.query_messages(
            where=expression("attachment_text_phrase", "nul\x00STRASSE")
        ).total_matches
        == 1
    )
    negative = phrase_db.query_messages(where=expression("attachment_text_phrase", "missing"))
    assert negative.indeterminate == 0
    with closing(sqlite3.connect(phrase_db.path)) as conn:
        conn.execute("UPDATE attachment_extractions SET extracted_at='new-generation'")
        conn.commit()
    stale = phrase_db.query_messages(where=expression("attachment_text_phrase", "nul\x00STRASSE"))
    assert stale.total_matches == 0 and stale.indeterminate == 1
    with closing(sqlite3.connect(phrase_db.path)) as conn:
        conn.execute("UPDATE attachments SET text_complete=0")
        writer.rebuild_attachments(conn.cursor(), claimant_of("three"), True)
        conn.commit()
    assert (
        phrase_db.query_messages(
            where=expression("attachment_text_phrase", "strasse")
        ).total_matches
        == 1
    )
    assert (
        phrase_db.query_messages(
            where=expression("attachment_text_phrase", "missing")
        ).indeterminate
        == 1
    )
    with closing(sqlite3.connect(phrase_db.path)) as conn:
        conn.execute("UPDATE attachments SET extraction_deferred_at='synthetic-deferred'")
        conn.commit()
    page = phrase_db.query_messages(where=expression("attachment_text_phrase", "strasse"))
    assert page.total_matches == 0 and page.indeterminate == 1


def test_short_membership_never_matches_inside_a_fold_expansion(phrase_db):
    assert phrase_db.query_messages(where=expression("body_phrase", "ß")).total_matches == 2
    assert phrase_db.query_messages(where=expression("body_phrase", " ")).total_matches == 2
    assert phrase_db.query_messages(where=expression("body_phrase", "\x00")).total_matches == 2


def test_deadline_is_a_fixed_error_and_source_text_is_not_logged(phrase_db, monkeypatch, caplog):
    import src.lib.literal as literal

    monkeypatch.setattr(literal, "DEADLINE_SECONDS", -1)
    with pytest.raises(InvalidFilterError, match="work limit exhausted") as error:
        phrase_db.query_messages(where=expression("body_phrase", "SYNTHETIC_PRIVATE_QUERY"))
    assert "SYNTHETIC_PRIVATE_QUERY" not in str(error.value) + caplog.text


def test_misaligned_overlapping_long_matches_take_linear_operand_work():
    work = []

    class Operand(str):
        def __getitem__(self, key):
            work.append(1)
            return super().__getitem__(key)

    text = "ΐ" * 7000 + " twenty ordinary synthetic words " * 20
    needle = Operand(("\u0308\u0301ι" * 334)[:1000])
    assert not contains(text, needle, [(0, len(text))])
    assert len(work) < 10 * (len(text.casefold()) + len(needle))


def test_wire_reports_snapshot_scope_unknown_evidence_and_authority_notice(phrase_db, fake_server):
    import asyncio

    from src.tools.retrieval import register_retrieval_tools

    register_retrieval_tools(fake_server, phrase_db)
    result = asyncio.run(
        fake_server.tools["query_messages"](
            where=Where.model_validate({"all": [{"leaf": "body_phrase", "value": "strasse"}]})
        )
    )
    assert result.structured_content["population_scope"] == "local_indexed_snapshot"
    assert result.structured_content["total_matches"] == 2
    assert result.structured_content["indeterminate"] == 1
    text = "\n".join(item.text for item in result.content)
    assert "literal source evidence or index incomplete" in text
    assert "local indexed snapshot" in text
    out = asyncio.run(
        fake_server.tools["query_messages"](
            where=Where.model_validate({"all": [{"leaf": "authority_class", "value": "vendor"}]})
        )
    )
    assert "unknown" in out.structured_content["authority_notice"]


def test_unicode_version_mismatch_is_unknown_not_absent(phrase_db):
    with closing(sqlite3.connect(phrase_db.path)) as conn:
        conn.execute(
            "UPDATE literal_state SET unicode_version='different' WHERE claimant_id=?",
            (claimant_of("three"),),
        )
        conn.commit()
    page = phrase_db.query_messages(where=expression("body_phrase", "strasse"))
    assert (page.total_matches, page.indeterminate) == (1, 2)
    entry = phrase_db.get_mailbox_status()["query_capabilities"]["body_phrase"]
    assert entry["unassessed_messages"] == 1


@pytest.mark.parametrize("limit", ["MAX_VERIFICATION_BYTES", "MAX_REQUEST_CANDIDATES"])
def test_shared_admission_budget_precedes_verification(phrase_db, monkeypatch, limit):
    import src.lib.literal as literal

    monkeypatch.setattr(literal, limit, 1)
    monkeypatch.setattr(literal, "contains", lambda *a: pytest.fail("payload verification ran"))
    with pytest.raises(InvalidFilterError, match="work limit exhausted"):
        phrase_db.query_messages(where=expression("body_phrase", "strasse"))


def test_foreign_cursor_is_refused_before_phrase_source_reads(phrase_db, monkeypatch):
    import src.lib.literal as literal

    first = phrase_db.query_messages(where=expression("body_phrase", "strasse"), limit=1)
    monkeypatch.setattr(
        literal, "contains", lambda *a: pytest.fail("source read before cursor validation")
    )
    with pytest.raises(InvalidFilterError):
        phrase_db.query_messages(
            where=expression("body_phrase", "different"), cursor=first.next_cursor
        )


@pytest.mark.parametrize("scope", ["body_phrase", "attachment_text_phrase"])
@pytest.mark.parametrize("stop", ["space", "membership"])
def test_resource_caps_keep_positives_and_unknown_negatives(
    empty_db, monkeypatch, caplog, scope, stop
):
    from tests.conftest import _insert_attachment, _insert_extraction

    with closing(sqlite3.connect(empty_db.path)) as conn:
        _insert_message(
            conn,
            message_id="capped",
            thread_id="synthetic-thread",
            sent_at="2024-01-01T00:00:00+00:00",
        )
        text = (
            "retained_literal_marker " + "synthetic ordinary words " * 1000 + " tail_literal_marker"
        )
        if scope == "attachment_text_phrase":
            add_source(conn, "capped", [("synthetic body", "body")])
            _insert_attachment(
                conn,
                attachment_id="payload",
                message_id="capped",
                thread_id="synthetic-thread",
                filename="synthetic.pdf",
            )
            _insert_extraction(
                conn, attachment_id="payload", extracted_text=text, extractor="pdf-digital@8"
            )
            conn.execute("UPDATE attachments SET text_complete=1,text_extractor='pdf-digital@8'")
            conn.execute("UPDATE messages SET has_attachments=1,attachment_rows_current=1")
        constructed = []
        original = writer._tokens

        def tokens(*args):
            constructed.append(1)
            return original(*args)

        monkeypatch.setattr(writer, "_tokens", tokens)
        if stop == "space":
            monkeypatch.setattr(writer, "_storage_available", lambda cur: not constructed)
        else:
            monkeypatch.setattr(writer, "MAX_MEMBERSHIP", 700000)
        if scope == "body_phrase":
            add_source(conn, "capped", [(text, "body")])
        else:
            writer.rebuild_attachments(conn.cursor(), claimant_of("capped"), True)
        conn.commit()
    assert len(constructed) == 1
    assert "retained_literal_marker" not in caplog.text
    for reader in ("query_messages", "query_threads", "aggregate_messages"):
        for needle, negate, expected in [
            ("retained_literal_marker", False, (1, 0)),
            ("tail_literal_marker", False, (0, 1)),
            ("tail_literal_marker", True, (0, 1)),
        ]:
            args = {"where": expression(scope, needle, negate)}
            if reader == "aggregate_messages":
                args["group_by"] = "folder"
            result = getattr(empty_db, reader)(**args)
            observed = (
                (result.total_matching_messages, result.indeterminate_messages)
                if reader == "query_threads"
                else (result.total_matches, result.indeterminate)
            )
            assert observed == expected, (scope, stop, reader, needle, negate)

    # A later normal reparse can assess the whole source when capacity permits.
    monkeypatch.setattr(writer, "MAX_MEMBERSHIP", 16 * 1024 * 1024)
    monkeypatch.setattr(writer, "_storage_available", lambda cur: True)
    with closing(sqlite3.connect(empty_db.path)) as conn:
        if scope == "body_phrase":
            writer.rebuild_body(conn.cursor(), claimant_of("capped"))
        else:
            writer.rebuild_attachments(conn.cursor(), claimant_of("capped"), True)
        conn.commit()
    assert (
        empty_db.query_messages(where=expression(scope, "tail_literal_marker")).total_matches == 1
    )
    assert (
        empty_db.query_messages(where=expression(scope, "absent_literal_marker")).indeterminate == 0
    )
