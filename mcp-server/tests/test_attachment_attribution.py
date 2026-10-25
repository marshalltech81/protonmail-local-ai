"""#1347: attachment headers belong to the carrying message."""

import asyncio

import pytest
from src.lib.sqlite import Database
from src.tools.outputs import HEADER_CHAR_LIMIT, MAX_LISTED, message_headers
from src.tools.retrieval import _listed_attachment
from src.tools.search import register_search_tools

from tests.conftest import _insert_attachment, _insert_extraction, _insert_message, claimant_of
from tests.test_sqlite import _open_built_db_conn


@pytest.mark.parametrize("ambiguous", [0, 1, None])
@pytest.mark.parametrize("long", [False, True])
def test_attachment_headers_match_carrying_messages(
    tmp_path, fake_server, fake_embed, ambiguous, long
):
    conn, path = _open_built_db_conn(tmp_path, "attribution.db")
    subject = "Later message" if not long else "S" * 700
    senders = (
        ["Later Author <later@example.test>"]
        if not long
        else [f"{'N' * 700} <author{i}@example.test>" for i in range(MAX_LISTED + 2)]
    )
    _insert_message(
        conn,
        message_id="root",
        thread_id="t",
        subject="Root message",
        sent_at="2024-01-01T00:00:00+00:00",
        from_=["root@example.test"],
    )
    for mid, tid, subj, authors in [
        ("later", "t", subject, senders),
        ("forward", "f", "Forward message", ["forward@example.test"]),
    ]:
        _insert_message(
            conn,
            message_id=mid,
            thread_id=tid,
            subject=subj,
            sent_at="2024-02-01T00:01:00+00:00",
            from_=authors,
            sender_ambiguous=ambiguous,
            has_attachments=True,
        )
        _insert_attachment(
            conn,
            message_id=mid,
            thread_id=tid,
            attachment_id="same-payload",
            filename="report.pdf",
            occurrence_id=mid,
        )
    _insert_extraction(
        conn, attachment_id="same-payload", status="success", extracted_text="report"
    )
    conn.commit()
    conn.close()
    db = Database(str(path))
    register_search_tools(fake_server, db, fake_embed)
    result = asyncio.run(fake_server.tools["search_attachments"]())
    hits = {r["claimant_id"]: r for r in result.structured_content["results"]}
    listed = [
        _listed_attachment(a).model_dump(by_alias=True) for a in db.query_attachments().attachments
    ]
    for row in listed:
        hit = hits[row["claimant_id"]]
        msg = db.get_message_view(row["claimant_id"])
        headers = message_headers(msg.record).model_dump(by_alias=True)
        for key in ["subject", "from", "from_count", "sender_ambiguous"]:
            assert row[key] == hit[key] == headers[key]
        assert row["sent_at"] == "2024-02-01T00:01:00+00:00"
        assert "senders" not in hit and "date_last" not in hit
        if row["claimant_id"] == claimant_of("later"):
            assert hit["thread_subject"] == "Root message"
            assert len(row["from"]) == min(len(senders), MAX_LISTED)
            assert row["from_count"] == len(senders)
            assert row["subject"].startswith(subject[:HEADER_CHAR_LIMIT])
    assert len(listed) == 2
    text = db.get_attachment_text("later", 0, 100)
    assert _listed_attachment(text.record).model_dump(by_alias=True) in listed
