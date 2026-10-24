"""Synthetic recipient/MIME grouping evidence and output bounds (#1337)."""

import asyncio
import logging
import sqlite3
from contextlib import closing

import pytest
from src.lib.predicates import InvalidFilterError, Where, normalize_where
from src.lib.security import _LOGGABLE_TOOL_PARAMS
from src.lib.sqlite import AGGREGATE_DIMENSIONS, Database
from src.tools.retrieval import _NO_VALUE_LABELS

from tests.conftest import _insert_attachment, _insert_message
from tests.test_retrieval import _handlers, _text
from tests.test_sqlite import _open_built_db_conn


def leaf(value, negate=False):
    return normalize_where(
        Where.model_validate(
            {"all": [{"leaf": "attachment_content_type_is", "value": value, "negate": negate}]}
        )
    )


def build(tmp_path, *, current=1, manifest=1, subject_complete=1, types=()):
    conn, path = _open_built_db_conn(tmp_path, "dimensions.db")
    _insert_message(
        conn,
        message_id="m",
        thread_id="t",
        sent_at="2024-01-01T00:00:00+00:00",
        from_=["author@synthetic.test"],
        to=["a@one.test", "b@one.test"],
        cc=["c@one.test", "d@two.test"],
        has_attachments=bool(types),
        completeness={
            "attachments_manifest_complete": manifest,
            "subject_complete": subject_complete,
        },
    )
    for i, ct in enumerate(types):
        _insert_attachment(
            conn,
            message_id="m",
            thread_id="t",
            attachment_id=f"p{i}",
            filename=f"file{i}",
            content_type=ct,
        )
    conn.execute("UPDATE messages SET attachment_rows_current = ?", (current,))
    conn.commit()
    conn.close()
    return Database(str(path))


def test_recipient_positive_deduplicated_and_coverage(tmp_path):
    db = build(tmp_path)
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute("UPDATE messages SET cc_addresses_complete = NULL, sender_ambiguous = 1")
    page = db.aggregate_messages(group_by="recipient_domain")
    assert [(r.value, r.messages) for r in page.groups] == [("one.test", 1), ("two.test", 1)]
    assert page.total_matches == page.incomplete_recipient_messages == 1
    for r in page.groups:
        where = normalize_where(
            Where.model_validate(
                {"all": [{"leaf": "domain_is", "role": "visible_recipient", "value": r.value}]}
            )
        )
        assert db.query_messages(where=where).total_matches == r.messages


@pytest.mark.parametrize("current", [None, 0, 1])
@pytest.mark.parametrize("manifest", [None, 0, 1])
def test_current_inventory_truth_and_group_grid(tmp_path, current, manifest):
    db = build(
        tmp_path,
        current=current,
        manifest=manifest,
        types=["application/pdf", "application/pdf", "text/plain"],
    )
    page = db.aggregate_messages(group_by="attachment_content_type")
    positive = current == 1
    assert [(r.value, r.messages) for r in page.groups] == (
        [("application/pdf", 1), ("text/plain", 1)] if positive else [(None, 1)]
    )
    assert page.incomplete_attachment_type_messages == int(not (manifest == 1 and positive))
    assert db.query_messages(where=leaf(" APPLICATION/PDF ")).total_matches == int(positive)
    absent = db.query_messages(where=leaf("image/png"))
    assert absent.total_matches == 0
    assert absent.indeterminate == int(not (manifest == 1 and positive))
    negative = db.query_messages(where=leaf("image/png", True))
    assert negative.total_matches == int(manifest == 1 and positive)
    assert negative.indeterminate == absent.indeterminate
    if positive:
        assert [r.distinct_attachment_ids for r in page.groups] == [2, 1]


def test_unknown_memberships_include_filter_indeterminate(tmp_path):
    db = build(tmp_path, current=None, manifest=None, subject_complete=None)
    page = db.aggregate_messages(group_by="attachment_content_type", subject="missing")
    assert page.total_matches == page.attachment_inventory_unknown_messages == 0
    assert page.indeterminate == page.incomplete_attachment_type_messages == 1
    assert page.groups[0].messages == 0


def test_known_no_attachments_excludes_retained_rows(tmp_path):
    db = build(tmp_path, current=0, types=["application/pdf"])
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute("UPDATE messages SET has_attachments = 0")
    page = db.aggregate_messages(group_by="attachment_content_type")
    assert page.groups[0].value is None
    assert page.groups[0].distinct_attachment_ids == page.incomplete_attachment_type_messages == 0
    assert db.query_messages(where=leaf("application/pdf", True)).total_matches == 1


@pytest.mark.parametrize(
    "ct", ["application/a b", "application/a\tb", "application/a\0b", "/", "application/"]
)
def test_exact_stored_mime_evidence_not_grammar(tmp_path, ct):
    db = build(tmp_path, types=[ct])
    row = db.aggregate_messages(group_by="attachment_content_type").groups[0]
    assert row.value == ct and not row.value_clipped
    assert db.query_messages(where=leaf(ct)).total_matches == row.messages == 1


def test_mime_clipping_does_not_merge_groups_or_enter_cursor(tmp_path, monkeypatch):
    prefix = "application/" + "x" * 1_000_000
    db = build(tmp_path, types=[prefix + "a", prefix + "b"])
    connect = db._connect
    values = []

    def traced():
        conn = connect()

        class Cursor:
            def __init__(self, cur):
                self.cur = cur

            def fetchall(self):
                rows = self.cur.fetchall()
                values.extend(r["value"] for r in rows)
                return rows

        class Connection:
            def execute(self, sql, params):
                return Cursor(conn.execute(sql, params))

            def close(self):
                conn.close()

        return Connection()

    monkeypatch.setattr(db, "_connect", traced)
    p = db.aggregate_messages(group_by="attachment_content_type", limit=1)
    q = db.aggregate_messages(group_by="attachment_content_type", limit=1, cursor=p.next_cursor)
    assert p.total_groups == 2 and p.has_more and not q.has_more
    assert p.groups[0].value == q.groups[0].value
    assert all(r.value_clipped and len(r.value) == 500 for r in [*p.groups, *q.groups])
    assert [p.groups[0].distinct_attachment_ids, q.groups[0].distinct_attachment_ids] == [1, 1]
    assert all(len(v) <= 2001 for v in values)
    assert len(p.next_cursor) < 200


@pytest.mark.parametrize(
    "address,domain",
    [
        ("a@example.test", "example.test"),
        ("a@example.test\0x", None),
        ("a\0x@example.test", None),
        ("a@exa\0mple.test", None),
        ('"a@b"@example.test', "example.test"),
    ],
)
def test_recipient_domain_uses_predicate_text_semantics(tmp_path, address, domain):
    db = build(tmp_path)
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute("DELETE FROM message_participants WHERE role IN ('to','cc')")
        conn.execute(
            "INSERT INTO message_participants (claimant_id, role, address) "
            "SELECT claimant_id, 'to', ? FROM messages",
            (address,),
        )
    row = db.aggregate_messages(group_by="recipient_domain").groups[0]
    assert row.value == domain and row.messages == 1
    if domain:
        w = normalize_where(
            Where.model_validate(
                {"all": [{"leaf": "domain_is", "role": "visible_recipient", "value": domain}]}
            )
        )
        assert db.query_messages(where=w).total_matches == 1
    else:
        for negate in (False, True):
            w = normalize_where(
                Where.model_validate(
                    {
                        "all": [
                            {
                                "leaf": "domain_is",
                                "role": "visible_recipient",
                                "value": "example.test",
                                "negate": negate,
                            }
                        ]
                    }
                )
            )
            page = db.query_messages(where=w)
            assert page.total_matches == 0 and page.indeterminate == 1


def test_new_leaf_cap_and_schema_dependencies(tmp_path):
    db = build(tmp_path)
    with pytest.raises(InvalidFilterError):
        leaf(" " + "x" * 1000)
    assert (
        db.get_mailbox_status()["query_capabilities"]["attachment_content_type_is"]["status"]
        == "supported"
    )
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute("ALTER TABLE messages DROP COLUMN attachment_rows_current")
    entry = db.get_mailbox_status()["query_capabilities"]["attachment_content_type_is"]
    assert entry["status"] == "unavailable" and entry["available_messages"] is None


def test_dimension_metadata_covers_registry():
    assert set(_NO_VALUE_LABELS) == set(AGGREGATE_DIMENSIONS) - {"folder"}
    assert all(_LOGGABLE_TOOL_PARAMS["group_by"](d) for d in AGGREGATE_DIMENSIONS)


def test_wire_discloses_clipping_and_no_mail_in_logs(tmp_path, caplog, fake_server):
    marker = "SYNTHETIC_MIME_PRIVATE_MARKER"
    db = build(tmp_path, types=["application/" + marker * 50])
    fn = _handlers(fake_server, db)["aggregate_messages"]
    with caplog.at_level(logging.INFO):
        result = asyncio.run(fn(group_by="attachment_content_type"))
    assert result.structured_content["groups"][0]["value_clipped"]
    assert "clipped display labels" in _text(result)
    assert "'clipped_values': 1" in caplog.text and marker not in caplog.text


@pytest.mark.parametrize("dimension", ["recipient_domain", "attachment_content_type"])
def test_new_dimensions_expand_identities_only_after_page(tmp_path, monkeypatch, dimension):
    from tests.test_aggregate_periods import _wide_inventory

    visits = []
    for size in (40, 400):
        db = _wide_inventory(tmp_path, size)
        with closing(sqlite3.connect(db.path)) as conn, conn:
            conn.execute("UPDATE message_participants SET role = 'to' WHERE role = 'from'")
            if dimension == "attachment_content_type":
                for i, (oid,) in enumerate(
                    conn.execute(
                        "SELECT attachment_occurrence_id FROM attachments ORDER BY attachment_occurrence_id"
                    ).fetchall()
                ):
                    conn.execute(
                        "UPDATE attachments SET content_type = ? WHERE attachment_occurrence_id = ?",
                        (f"application/type{i:05d}", oid),
                    )
        connect = db._connect
        count = connections = 0
        uncapped = False
        statements = []

        def traced():
            nonlocal count, connections
            conn = connect()
            connections += 1

            def probe(value):
                nonlocal count
                count += 1
                return value

            conn.create_function("attachment_visit", 1, probe)
            conn.set_trace_callback(statements.append)

            class Connection:
                def execute(self, sql, params):
                    if uncapped:
                        sql = sql.replace(
                            "SELECT k.* FROM p CROSS JOIN k WHERE k.value IS p.value AND k.ok",
                            "SELECT k.* FROM k WHERE k.ok",
                        )
                    if dimension == "attachment_content_type":
                        sql = sql.replace(
                            "a.content_type IS kp.value",
                            "attachment_visit(a.content_type) IS kp.value",
                        )
                    else:
                        sql = sql.replace(
                            "COUNT(DISTINCT a.attachment_id)",
                            "COUNT(DISTINCT attachment_visit(a.attachment_id))",
                        )
                    return conn.execute(sql, params)

                def close(self):
                    conn.close()

            return Connection()

        with monkeypatch.context() as patch:
            patch.setattr(db, "_connect", traced)
            page = db.aggregate_messages(group_by=dimension, limit=2)
        assert page.total_groups == size and len(page.groups) == 2 and page.has_more
        assert all(
            g.distinct_attachment_ids == (1 if dimension == "attachment_content_type" else size)
            for g in page.groups
        )
        assert count == 3 * size  # returned groups plus one lookahead, not size squared
        assert connections == 1 and len(statements) == 1
        visits.append(count)
        if size == 40:
            # The same measurement fails on the former all-group expansion.
            uncapped = True
            count = 0
            with monkeypatch.context() as patch:
                patch.setattr(db, "_connect", traced)
                db.aggregate_messages(group_by=dimension, limit=2)
            assert count == size * size and count > visits[-1]
    assert visits[1] == 10 * visits[0]


def test_recipient_without_derivable_domain_does_not_add_null_membership(tmp_path):
    db = build(tmp_path)
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute(
            "INSERT INTO message_participants (claimant_id, role, address) "
            "SELECT claimant_id, 'to', ? FROM messages",
            ("a\0x@hidden.test",),
        )
    page = db.aggregate_messages(group_by="recipient_domain")
    assert [r.value for r in page.groups] == ["one.test", "two.test"]


def test_mime_decoder_substitution_is_disclosed(tmp_path):
    db = build(tmp_path, types=["application/pdf"])
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute(
            "UPDATE attachments SET content_type = CAST(? AS TEXT)", (b"application/\xff",)
        )
    row = db.aggregate_messages(group_by="attachment_content_type").groups[0]
    assert row.value == "application/\ufffd" and row.value_clipped


@pytest.mark.parametrize("dimension", ["recipient_domain", "attachment_content_type"])
def test_new_dimensions_keep_sender_only_breakdown(tmp_path, dimension):
    db = build(tmp_path)
    with pytest.raises(InvalidFilterError, match="period_breakdown"):
        db.aggregate_messages(group_by=dimension, period_breakdown=True)
    page = db.aggregate_messages(group_by=dimension, date_basis="sent", period="year")
    assert page.date_basis == "sent" and page.period == "year"
    assert all(r.active_periods == 1 for r in page.groups)
