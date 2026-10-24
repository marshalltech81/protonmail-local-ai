"""Migration checks only account metadata before switching configuration."""

import contextlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate  # noqa: E402

UUID = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
CONFIG = {"accounts": [{"scope_uuid": UUID, "provider": "proton-bridge", "enabled": True}]}
MARKER = "SYNTHETIC_PRIVATE_MIGRATION_MARKER"


class AccountCheck(unittest.TestCase):
    def test_live_check_uses_fixed_read_only_probe_and_configured_identity(self):
        with patch.object(
            migrate.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            migrate._check_live_account(CONFIG)
        args = run.call_args.args[0]
        self.assertEqual(args[:5], ["docker", "exec", "indexer", "python", "-c"])
        self.assertEqual(args[6:], [UUID, "proton-bridge"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_probe_failures_are_fixed_and_never_forward_output(self):
        for failure in [1, 2, 127, OSError(MARKER), subprocess.TimeoutExpired(MARKER, 30)]:
            with self.subTest(failure=type(failure).__name__):
                kwargs = (
                    {"side_effect": failure}
                    if isinstance(failure, Exception)
                    else {"return_value": subprocess.CompletedProcess([], failure, MARKER, MARKER)}
                )
                with patch.object(migrate.subprocess, "run", **kwargs):
                    with self.assertRaises(migrate.MigrateError) as error:
                        migrate._check_live_account(CONFIG)
                self.assertNotIn(MARKER, str(error.exception))
                self.assertNotIn(UUID, str(error.exception))

    def test_real_probe_checks_account_rows_without_writing_or_reading_mail(self):
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            for rows in [
                [(1, UUID, "proton-bridge")],
                [(1, OTHER, "proton-bridge")],
                [(1, UUID, "wrong")],
                [],
                [(1, UUID, "proton-bridge"), (2, OTHER, "proton-bridge")],
            ]:
                with self.subTest(rows=len(rows)):
                    path.unlink(missing_ok=True)
                    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                        conn.execute(
                            "CREATE TABLE accounts(id INTEGER,scope_uuid TEXT,provider TEXT)"
                        )
                        conn.executemany("INSERT INTO accounts VALUES(?,?,?)", rows)
                    before = path.read_bytes()
                    env = {
                        **os.environ,
                        "SQLITE_PATH": str(path),
                        "PYTHONPATH": str(root / "indexer"),
                    }
                    result = subprocess.run(
                        [sys.executable, "-c", migrate._CHECK_ACCOUNT, UUID, "proton-bridge"],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, 0 if rows == [(1, UUID, "proton-bridge")] else 2
                    )
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(path.read_bytes(), before)
            path.unlink()
            result = subprocess.run(
                [sys.executable, "-c", migrate._CHECK_ACCOUNT, UUID, "proton-bridge"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(path.exists())
