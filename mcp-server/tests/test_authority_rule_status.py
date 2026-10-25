"""Legacy applied-rule metadata is unknown, never zero (#1234)."""

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools


def test_status_without_applied_rule_metadata_reports_unknown(empty_db):
    metadata = empty_db.get_mailbox_status()["authority_rules"]
    assert metadata == {"configured": None, "rule_count": None, "classes": None}


@pytest.mark.parametrize(
    "configured,classes,filter_class,fragment",
    [
        (None, None, "vendor", "unknown"),
        (False, [], "vendor", "No authority rules file"),
        (True, [], "vendor", "No applied authority rule"),
        (True, ["counsel"], "vendor", "No applied authority rule"),
        (True, ["vendor"], "vendor", None),
        (True, ["vendor"], " vendor ", None),
        (False, [], "unclassified", None),
    ],
)
@pytest.mark.parametrize(
    "tool", ["query_messages", "query_threads", "aggregate_messages", "search_emails"]
)
def test_filter_notices_use_applied_classes_without_claiming_mail_absence(
    empty_db, fake_server, configured, classes, filter_class, fragment, tool
):
    if configured is not None:
        with closing(sqlite3.connect(empty_db.path)) as conn:
            conn.execute(
                "INSERT INTO authority_rule_state VALUES (1, ?, ?, ?)",
                (int(configured), len(classes), json.dumps(classes)),
            )
            conn.commit()
    register_retrieval_tools(fake_server, empty_db)
    register_search_tools(fake_server, empty_db, embed_client=None)
    args = {"authority_class": filter_class}
    if tool == "search_emails":
        args.update(query="synthetic", mode="keyword")
    if tool == "aggregate_messages":
        args["group_by"] = "sender_address"
    out = asyncio.run(fake_server.tools[tool](**args))
    note = out.structured_content["authority_notice"]
    if fragment:
        assert fragment in note
        assert note in out.content[0].text
    else:
        assert note is None
    assert "SYNTHETIC_PRIVATE" not in out.content[0].text


@pytest.mark.parametrize("pending", [False, True])
def test_reparse_explanation_depends_on_metadata_not_just_queue(seeded_db, fake_server, pending):
    from tests.conftest import write_ingestion
    from tests.test_system_handlers import _handler

    write_ingestion(seeded_db.path, jobs=(("queued", 0, None, "reparse"),) if pending else ())
    out = asyncio.run(_handler(fake_server, seeded_db)())
    text = "\n".join(item.text for item in out.content)
    assert ("queue membership alone does not establish that metadata is missing" in text) is pending
    assert out.structured_content["authority_rules"] == {
        "configured": None,
        "rule_count": None,
        "classes": None,
    }


def test_legacy_table_absence_is_unknown_and_status_logs_no_patterns(empty_db, caplog):
    with closing(sqlite3.connect(empty_db.path)) as conn:
        conn.execute("DROP TABLE authority_rule_state")
        conn.commit()
    stats = empty_db.get_mailbox_status()
    assert stats["authority_rules"] == {"configured": None, "rule_count": None, "classes": None}
    assert "unknown" in empty_db.authority_rule_notice("vendor")
    # Only fixed text and class counts are public; source rules never enter this table.
    assert "SYNTHETIC_PRIVATE" not in str(stats["authority_rules"]) + caplog.text
