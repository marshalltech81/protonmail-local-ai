"""Tests for src/maildir.py — flag parsing, Maildir uniq resolution, and
mbsync's last-sync stamp."""

from pathlib import Path

import pytest
from src.maildir import (
    FLAG_SEPARATOR,
    SYNC_STAMP_NAME,
    MessageState,
    SyncStamp,
    get_uniq,
    is_trashed,
    message_state,
    parse_flags,
    parse_sync_stamp_rename,
    read_sync_stamp,
    resolve_current_path,
)


class TestParseFlags:
    def test_returns_empty_set_for_filename_without_flag_suffix(self):
        assert parse_flags(Path("1700000000.M1P2Q3.host")) == set()

    def test_parses_single_flag(self):
        assert parse_flags(Path("1700000000.M1.host:2,S")) == {"S"}

    def test_parses_multiple_flags(self):
        assert parse_flags(Path("1700000000.M1.host:2,SRF")) == {"S", "R", "F"}

    def test_parses_trashed_flag(self):
        assert parse_flags(Path("1700000000.M1.host:2,ST")) == {"S", "T"}

    def test_accepts_string_input(self):
        assert parse_flags("1700000000.M1.host:2,T") == {"T"}


class TestIsTrashed:
    def test_false_when_no_flag_suffix(self):
        assert is_trashed(Path("msg.host")) is False

    def test_false_when_t_not_in_flags(self):
        assert is_trashed(Path("msg.host:2,SR")) is False

    def test_true_when_t_in_flags(self):
        assert is_trashed(Path("msg.host:2,ST")) is True

    def test_true_when_t_is_sole_flag(self):
        assert is_trashed(Path("msg.host:2,T")) is True


class TestMessageState:
    """Read / flagged / replied state from the ``:2,<flags>`` suffix,
    through the same ``parse_flags`` the trash check uses."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # mbsync delivers unseen mail to new/ without a suffix.
            ("1700000000.M1.host", MessageState(seen=False, flagged=False, replied=False)),
            ("1700000000.M1.host:2,", MessageState(seen=False, flagged=False, replied=False)),
            ("1700000000.M1.host:2,S", MessageState(seen=True, flagged=False, replied=False)),
            ("1700000000.M1.host:2,F", MessageState(seen=False, flagged=True, replied=False)),
            ("1700000000.M1.host:2,RS", MessageState(seen=True, flagged=False, replied=True)),
            ("1700000000.M1.host:2,DFPRST", MessageState(seen=True, flagged=True, replied=True)),
            # Trashed is not read: T says nothing about S.
            ("1700000000.M1.host:2,T", MessageState(seen=False, flagged=False, replied=False)),
            ("1700000000.M1.host:2,ST", MessageState(seen=True, flagged=False, replied=False)),
            # Unknown letters and lowercase keyword letters are ignored,
            # and flags are case-sensitive.
            ("1700000000.M1.host:2,Xabc", MessageState(seen=False, flagged=False, replied=False)),
            ("1700000000.M1.host:2,sfr", MessageState(seen=False, flagged=False, replied=False)),
            ("1700000000.M1.host:2,aSz", MessageState(seen=True, flagged=False, replied=False)),
            # Only the last separator carries flags.
            ("weird:2,SF.host:2,R", MessageState(seen=False, flagged=False, replied=True)),
        ],
    )
    def test_state_from_filename(self, name, expected):
        assert message_state(Path("/maildir/INBOX/cur") / name) == expected

    def test_directory_names_do_not_count(self):
        assert message_state("/maildir/x:2,SFR/cur/1700000000.M1.host") == MessageState(
            seen=False, flagged=False, replied=False
        )


class TestGetUniq:
    def test_returns_full_name_when_no_flag_suffix(self):
        assert get_uniq(Path("1700000000.M1P2.host")) == "1700000000.M1P2.host"

    def test_strips_flag_suffix(self):
        assert get_uniq(Path("1700000000.M1P2.host:2,SR")) == "1700000000.M1P2.host"

    def test_strips_empty_flag_suffix(self):
        assert get_uniq(Path("1700000000.M1P2.host" + FLAG_SEPARATOR)) == "1700000000.M1P2.host"


class TestResolveCurrentPath:
    def test_returns_stored_path_when_still_present(self, tmp_path: Path):
        f = tmp_path / "msg.host:2,S"
        f.write_text("data")
        assert resolve_current_path(f) == f

    def test_finds_renamed_file_with_same_uniq(self, tmp_path: Path):
        stored = tmp_path / "msg.host:2,S"
        actual = tmp_path / "msg.host:2,ST"
        actual.write_text("data")
        # stored does not exist on disk; actual has the same uniq
        assert resolve_current_path(stored) == actual

    def test_returns_none_when_file_fully_gone(self, tmp_path: Path):
        stored = tmp_path / "msg.host:2,S"
        assert resolve_current_path(stored) is None

    def test_returns_none_when_parent_directory_missing(self, tmp_path: Path):
        stored = tmp_path / "missing_dir" / "msg.host:2,S"
        assert resolve_current_path(stored) is None

    def test_does_not_match_different_uniq(self, tmp_path: Path):
        stored = tmp_path / "msg1.host:2,S"
        (tmp_path / "msg2.host:2,S").write_text("other")
        assert resolve_current_path(stored) is None

    def test_matches_bare_uniq_without_flag_suffix(self, tmp_path: Path):
        stored = tmp_path / "msg.host:2,S"
        actual = tmp_path / "msg.host"
        actual.write_text("data")
        assert resolve_current_path(stored) == actual

    def test_finds_file_promoted_from_new_to_cur(self, tmp_path: Path):
        # mbsync moved the file from INBOX/new to INBOX/cur while indexer
        # was offline. The scan must follow the sibling or the reconciler
        # will treat a live message as missing and tombstone it.
        folder = tmp_path / "INBOX"
        (folder / "new").mkdir(parents=True)
        cur = folder / "cur"
        cur.mkdir()
        stored = folder / "new" / "msg.host"
        actual = cur / "msg.host:2,S"
        actual.write_text("data")
        assert resolve_current_path(stored) == actual

    def test_finds_file_demoted_from_cur_to_new(self, tmp_path: Path):
        folder = tmp_path / "INBOX"
        new = folder / "new"
        new.mkdir(parents=True)
        (folder / "cur").mkdir()
        stored = folder / "cur" / "msg.host:2,S"
        actual = new / "msg.host"
        actual.write_text("data")
        assert resolve_current_path(stored) == actual

    def test_returns_none_when_missing_in_both_new_and_cur(self, tmp_path: Path):
        folder = tmp_path / "INBOX"
        (folder / "new").mkdir(parents=True)
        (folder / "cur").mkdir()
        stored = folder / "cur" / "msg.host:2,S"
        assert resolve_current_path(stored) is None


class TestReadSyncStamp:
    def test_missing_stamp_means_no_sync_recorded(self, tmp_path):
        assert read_sync_stamp(tmp_path) is None

    def test_reads_completion_time_as_utc_and_interval(self, tmp_path):
        (tmp_path / SYNC_STAMP_NAME).write_text(
            '{"completed_at": "2026-09-28T12:00:00Z", "sync_interval_secs": 60}\n'
        )
        assert read_sync_stamp(tmp_path) == SyncStamp(
            completed_at="2026-09-28T12:00:00+00:00", sync_interval_secs=60
        )

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            "[]",
            '{"sync_interval_secs": 60}',
            '{"completed_at": "yesterday", "sync_interval_secs": 60}',
            '{"completed_at": "2026-09-28T12:00:00", "sync_interval_secs": 60}',
            '{"completed_at": "2026-09-28T12:00:00Z", "sync_interval_secs": 0}',
            '{"completed_at": "2026-09-28T12:00:00Z", "sync_interval_secs": "60"}',
            '{"completed_at": "2026-09-28T12:00:00Z", "sync_interval_secs": true}',
        ],
    )
    def test_malformed_stamp_raises(self, tmp_path, content):
        """A stamp that cannot be read must not pass for a sync time:
        the caller logs it and records no sync."""
        (tmp_path / SYNC_STAMP_NAME).write_text(content)
        with pytest.raises(ValueError):
            read_sync_stamp(tmp_path)


class TestParseSyncStampRename:
    def test_reads_the_sync_from_the_temporary_name(self):
        assert parse_sync_stamp_rename(
            "/maildir/.mbsync-last-sync.2026-09-28T12:00:00Z.60.tmp"
        ) == SyncStamp(completed_at="2026-09-28T12:00:00+00:00", sync_interval_secs=60)

    @pytest.mark.parametrize(
        "src",
        [
            "/maildir/other.tmp",
            "/maildir/.mbsync-last-sync.json.tmp",
            "/maildir/.mbsync-last-sync.yesterday.60.tmp",
            "/maildir/.mbsync-last-sync.2026-09-28T12:00:00.60.tmp",
            "/maildir/.mbsync-last-sync.2026-09-28T12:00:00Z.0.tmp",
        ],
    )
    def test_anything_else_is_not_a_sync(self, src):
        assert parse_sync_stamp_rename(src) is None
