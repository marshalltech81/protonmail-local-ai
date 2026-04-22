"""Tests for src/maildir.py — flag parsing and Maildir uniq resolution."""

from pathlib import Path

from src.maildir import (
    FLAG_SEPARATOR,
    get_uniq,
    is_trashed,
    parse_flags,
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
