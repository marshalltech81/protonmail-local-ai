"""#1568: no code reads the per-copy columns the v17 migration drops.

v17 moves folder, locator and Maildir flags to ``message_occurrences``
and drops ``messages.filepath/folder/seen/flagged/replied`` and
``message_thread_map.filepath``. A statement still naming one fails only
when it runs, so a path no test executes would fail on a live index.

The readers are derived from the source, not from a list of the call
sites that were moved: every SQL string of the indexer and mcp-server
(``src/`` and the benchmark script that writes an index) is scanned for
a reference to a dropped column through the table's name, an alias the
source gives it, an ``INSERT`` column list, an ``UPDATE ... SET``, a
column interpolated after an alias, or a bare column name in a statement
on the table that no other table in it could own. Which other tables own
a column comes from the real schema.
"""

import ast
import re
from pathlib import Path

from src.database import Database

from tests.conftest import TEST_ACCOUNT

_REPO = Path(__file__).resolve().parents[2]
_ROOTS = (
    _REPO / "indexer" / "src",
    _REPO / "mcp-server" / "src",
    _REPO / "mcp-server" / "scripts",
)
# Migrations up to v17 describe the schema they replace: they create,
# copy and drop these columns by design.
_LAST_HISTORICAL_MIGRATION = 17

DROPPED = {
    "messages": ("filepath", "folder", "seen", "flagged", "replied"),
    "message_thread_map": ("filepath",),
}

# Interpolated columns read after a messages alias (``m.{name}``): each
# names a completeness flag, never a dropped column. Matched on the
# exact source text so a new interpolation must be reviewed here.
_INTERPOLATED_ALLOWED = {
    "m.{ROLE_COMPLETE_COLUMNS[role]}",  # predicates: per-role address completeness flag
    "m.{complete}",  # predicates: a ``*_complete`` flag named by its caller
    "m.{flag}",  # predicates: _coverage_flags, always a *_complete flag (#1627)
}

_TABLE_REF = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)\b(?:\s+(?:AS\s+)?(\w+))?", re.I)
_SQL_WORDS = {
    "and", "as", "cross", "default", "except", "group", "having", "indexed",
    "inner", "intersect", "join", "left", "limit", "natural", "not", "on",
    "or", "order", "returning", "select", "set", "union", "using", "values",
    "where", "window", "with",
}  # fmt: skip


def _migration_number(path: Path) -> int | None:
    match = re.match(r"(\d{4})_", path.name)
    return int(match.group(1)) if match and path.parent.name == "migrations" else None


def _sources() -> list[Path]:
    files = []
    for root in _ROOTS:
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".py", ".sql"):
                continue
            number = _migration_number(path)
            if number is not None and number <= _LAST_HISTORICAL_MIGRATION:
                continue
            files.append(path)
    return files


def _strings(path: Path) -> list[tuple[int, str, str]]:
    """Every string in ``path`` as ``(line, text, source)``: a ``.sql``
    file whole; a Python string constant, or an f-string with each
    interpolation kept as its source text in braces."""
    text = path.read_text()
    if path.suffix == ".sql":
        return [(1, text, text)]
    found = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(str(value.value))
                else:
                    assert isinstance(value, ast.FormattedValue)
                    parts.append("{" + ast.unparse(value.value) + "}")
            joined = "".join(parts)
            found.append((node.lineno, joined, joined))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value, node.value))
    return found


def _schema_columns(tmp_path: Path) -> dict[str, set[str]]:
    db = Database(tmp_path / "schema.db", account=TEST_ACCOUNT)
    try:
        conn = db._conn
        tables = [
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            table: {row[1] for row in conn.execute(f"PRAGMA table_xinfo('{table}')")}
            for table in tables
        }
    finally:
        db.close()


def find_dropped_column_references(
    sources: list[Path], schema: dict[str, set[str]], repo: Path = _REPO
) -> list[str]:
    strings = [(path, *entry) for path in sources for entry in _strings(path)]
    aliases: dict[str, set[str]] = {}
    for _path, _line, text, _source in strings:
        for match in _TABLE_REF.finditer(text):
            table, alias = match.group(1), match.group(2)
            if alias and alias.lower() not in _SQL_WORDS:
                aliases.setdefault(alias, set()).add(table)

    found = []
    for path, line, text, _source in strings:
        where = f"{path.relative_to(repo)}:{line}"
        tables_here = {m.group(1) for m in _TABLE_REF.finditer(text)}
        for table, columns in DROPPED.items():
            names = [table] + [a for a, owners in aliases.items() if table in owners]
            for name in names:
                for column in columns:
                    # ``{m.folder}`` is a Python interpolation, not SQL.
                    if re.search(rf"(?<![{{\w.]){name}\.{column}\b", text):
                        found.append(f"{where}: {name}.{column}")
                for match in re.finditer(rf"\b{name}\.(\{{[^}}]*\}})", text):
                    reference = f"{name}.{match.group(1)}"
                    if reference in _INTERPOLATED_ALLOWED:
                        continue
                    found.append(f"{where}: interpolated {reference}")
            for match in re.finditer(
                rf"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+{table}\s*\(([^)]*)\)", text, re.I
            ):
                for column in columns:
                    if re.search(rf"\b{column}\b", match.group(1)):
                        found.append(f"{where}: INSERT INTO {table} ({column})")
            for match in re.finditer(
                rf"\bUPDATE\s+{table}\s+SET\b(.*?)(?:\bWHERE\b|$)", text, re.I | re.S
            ):
                for column in columns:
                    if re.search(rf"\b{column}\s*=", match.group(1)):
                        found.append(f"{where}: UPDATE {table} SET {column}")
            if table not in tables_here:
                continue
            # A bare column in a statement on the table: a reference
            # unless another table of the statement owns that name.
            others = tables_here - {table}
            for column in columns:
                if any(column in schema.get(other, ()) for other in others):
                    continue
                if re.search(rf"(?<![.\w]){column}(?!\w)", text) and re.search(
                    r"\b(SELECT|INSERT|UPDATE|DELETE)\b", text, re.I
                ):
                    found.append(f"{where}: bare {column} in a statement on {table}")
    return sorted(set(found))


def test_no_source_reads_a_dropped_column(tmp_path):
    schema = _schema_columns(tmp_path)
    # The columns are gone from the schema the scan checks against.
    for table, columns in DROPPED.items():
        assert not schema[table] & set(columns), table
    assert find_dropped_column_references(_sources(), schema) == []


def test_the_scan_finds_each_reference_shape(tmp_path):
    """The scan fails on the shapes the v14 readers used (shown failing
    on the pre-#1568 source before it was trusted)."""
    source = tmp_path / "pkg" / "shapes.py"
    source.parent.mkdir()
    source.write_text(
        "A = 'SELECT m.folder FROM messages m WHERE m.claimant_id = ?'\n"
        "B = 'INSERT INTO messages (claimant_id, seen) VALUES (?, ?)'\n"
        "C = 'UPDATE messages SET flagged = ? WHERE claimant_id = ?'\n"
        "D = 'SELECT DISTINCT folder FROM messages'\n"
        "E = 'SELECT filepath FROM message_thread_map WHERE thread_id = ?'\n"
        "def f(column):\n"
        "    return f'm.{column} = ?'\n"
        "G = 'SELECT 1 FROM messages WHERE messages.replied = 1'\n"
        "H = 'JOIN message_thread_map t ON t.thread_id = x WHERE t.filepath = ?'\n"
    )
    found = find_dropped_column_references([source], _schema_columns(tmp_path), tmp_path)
    assert found == [
        "pkg/shapes.py:1: m.folder",
        "pkg/shapes.py:2: INSERT INTO messages (seen)",
        "pkg/shapes.py:2: bare seen in a statement on messages",
        "pkg/shapes.py:3: UPDATE messages SET flagged",
        "pkg/shapes.py:3: bare flagged in a statement on messages",
        "pkg/shapes.py:4: bare folder in a statement on messages",
        "pkg/shapes.py:5: bare filepath in a statement on message_thread_map",
        "pkg/shapes.py:7: interpolated m.{column}",
        "pkg/shapes.py:8: messages.replied",
        "pkg/shapes.py:9: t.filepath",
    ]


def test_the_scan_ignores_columns_other_tables_own(tmp_path):
    source = tmp_path / "pkg" / "fine.py"
    source.parent.mkdir()
    source.write_text(
        "A = 'SELECT o.folder, o.seen FROM message_occurrences o JOIN messages m "
        "ON m.claimant_id = o.claimant_id'\n"
        "B = 'SELECT folder FROM threads JOIN messages m ON m.thread_id = threads.thread_id'\n"
        "C = 'SELECT filepath FROM indexing_jobs'\n"
        "D = 'messages filed in a folder are seen'\n"
        "def g(m):\n"
        "    return f'{m.folder} (+{m.seen} copies)'\n"
    )
    assert find_dropped_column_references([source], _schema_columns(tmp_path), tmp_path) == []
