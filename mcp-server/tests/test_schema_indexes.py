"""The mcp-server test schema carries every index the indexer creates on a
table it mirrors, with the same definition (#1213).

Without the indexes, SQLite plans the attachment lanes quadratically and a
work-bound test can pass or fail depending on which SQLite runs it. The
indexer is a separate project, so its schema is read from its source: every
``CREATE INDEX`` on a table ``conftest.py`` builds must also be built there,
with the same uniqueness, columns and partial predicate.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_INDEX = re.compile(
    r"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)\s*(\([^)]*\))([^;\"]*)",
    re.IGNORECASE,
)
_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)


def _normal(text: str) -> str:
    return " ".join(text.split())


def _definitions(source: str) -> dict[tuple[str, str], tuple[bool, str, str]]:
    """Each index by (name, table): (unique, columns, partial predicate)."""
    found = {}
    for unique, name, table, columns, rest in _INDEX.findall(source):
        # The predicate runs to the end of the statement; a SQL comment ends it.
        predicate = _normal(rest).split(" -- ")[0].strip()
        found[(name, table)] = (bool(unique), _normal(columns), predicate)
    return found


def test_every_indexer_index_on_a_mirrored_table_matches_the_test_schema():
    indexer_source = (_ROOT / "indexer" / "src" / "database.py").read_text()
    conftest_source = (Path(__file__).parent / "conftest.py").read_text()
    mirrored = set(_TABLE.findall(conftest_source))
    indexer = {k: v for k, v in _definitions(indexer_source).items() if k[1] in mirrored}
    test_schema = _definitions(conftest_source)
    assert indexer, "no indexer index found: the source path or pattern moved"
    mismatched = {k: (v, test_schema.get(k)) for k, v in indexer.items() if test_schema.get(k) != v}
    assert not mismatched, mismatched
