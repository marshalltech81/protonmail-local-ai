"""SQLite schema migration runner.

Migration files live next to this module as ``NNNN_<slug>.sql`` where
``NNNN`` is the target schema version (zero-padded, four digits) and
``<slug>`` is a short snake_case description. Each file contains the
SQL needed to upgrade FROM version ``NNNN - 1`` TO version ``NNNN``.

Fresh installs apply ``Database._apply_initial_schema`` directly and
stamp the current ``SCHEMA_VERSION`` — they skip the migration files
entirely. Migrations only run on existing databases that need to catch
up to a newer ``SCHEMA_VERSION``.

Each migration runs inside its own ``BEGIN IMMEDIATE`` / ``COMMIT`` so
a failing migration leaves the database stamped at the last
successfully applied version. The next startup retries only from the
failing migration onward.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def discover_migrations(directory: Path) -> list[tuple[int, Path]]:
    """Return ``(version, path)`` pairs for every migration file in ``directory``.

    Sorted ascending by version. Raises ``RuntimeError`` if any
    filename does not match the expected ``NNNN_<slug>.sql`` shape, if
    a version of ``0000`` appears (reserved — pre-runner state), or if
    two files share a version.
    """
    found: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILENAME_RE.match(path.name)
        if match is None:
            raise RuntimeError(
                f"invalid migration filename: {path.name!r} "
                "(expected NNNN_<slug>.sql with snake_case slug)"
            )
        version = int(match.group(1))
        if version <= 0:
            raise RuntimeError(
                f"invalid migration version {version} in {path.name!r} (versions start at 1)"
            )
        found.append((version, path))
    found.sort(key=lambda pair: pair[0])
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(
            f"duplicate migration version(s) in {directory}: "
            f"{[v for v in versions if versions.count(v) > 1]}"
        )
    return found


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements at top-level ``;``.

    Handles ``--`` line comments and single-quoted string literals
    (with the SQLite ``''`` escape). Block comments (``/* ... */``)
    are intentionally unsupported — keep migration files simple.

    This exists because ``sqlite3.Connection.executescript`` issues an
    implicit ``COMMIT`` before running, which breaks per-migration
    transactional atomicity. Running statements one at a time via
    ``Connection.execute`` inside an explicit ``BEGIN`` / ``COMMIT``
    keeps the whole migration atomic.
    """
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_string:
            current.append(ch)
            if ch == "'":
                # SQLite escapes a single quote inside a string by
                # doubling it (``''``). Keep the second quote in the
                # buffer and stay inside the string.
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    current.append(sql[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            current.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            # Line comment — skip up to (but not past) the newline so
            # the newline itself stays as a statement separator hint.
            while i < len(sql) and sql[i] != "\n":
                i += 1
            continue
        if ch == ";":
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    final = "".join(current).strip()
    if final:
        statements.append(final)
    return statements


def apply_pending(
    conn: sqlite3.Connection,
    *,
    current_version: int,
    target_version: int,
    migration_dir: Path,
) -> list[int]:
    """Apply migrations to advance ``schema_version`` to ``target_version``.

    The connection must already have a ``schema_version`` table with a
    single row holding ``current_version``. Each applied migration is
    bracketed by its own ``BEGIN IMMEDIATE`` / ``COMMIT``; a failure
    rolls back only that migration, leaving the schema stamped at the
    last successful version.

    Returns the versions that were applied, in order. Returns an empty
    list when ``current_version >= target_version`` (no-op for fresh
    installs, downgrade attempts, and steady-state startups).
    """
    if current_version >= target_version:
        return []

    available = discover_migrations(migration_dir)
    pending = [(v, p) for v, p in available if current_version < v <= target_version]
    expected = list(range(current_version + 1, target_version + 1))
    actual = [v for v, _ in pending]
    if actual != expected:
        raise RuntimeError(
            f"migration sequence broken between v{current_version} and "
            f"v{target_version}: expected files for versions {expected}, "
            f"found {actual}"
        )

    applied: list[int] = []
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        statements = _split_statements(sql)
        log.info(
            "Applying migration v%d (%s, %d statement(s))", version, path.name, len(statements)
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied.append(version)
        log.info("Migration v%d applied", version)
    return applied
