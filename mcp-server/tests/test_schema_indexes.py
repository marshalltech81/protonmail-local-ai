"""The mcp-server test schema carries every index the indexer creates on a
table it mirrors (#1213).

Without the indexes, SQLite plans the attachment lanes quadratically and a
work-bound test can pass or fail depending on which SQLite runs it. The
indexer is a separate project, so its schema is read from its source: every
``CREATE INDEX`` on a table ``conftest.py`` builds must also be built there.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_INDEX = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)\s*\(",
    re.IGNORECASE,
)
_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)


def _indexes(source: str) -> set[tuple[str, str]]:
    return set(_INDEX.findall(source))


def test_every_indexer_index_on_a_mirrored_table_is_in_the_test_schema():
    indexer_source = (_ROOT / "indexer" / "src" / "database.py").read_text()
    conftest_source = (Path(__file__).parent / "conftest.py").read_text()
    mirrored = set(_TABLE.findall(conftest_source))
    indexer = {(name, table) for name, table in _indexes(indexer_source) if table in mirrored}
    missing = sorted(indexer - _indexes(conftest_source))
    assert indexer, "no indexer index found: the source path or pattern moved"
    assert not missing, missing
