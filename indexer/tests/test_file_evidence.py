"""Bound actual verification work, including mutation during a read."""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from src.file_evidence import FolderInventory, file_identity, read_digest


def test_growth_cannot_make_a_hash_read_past_its_admitted_size(tmp_path, monkeypatch):
    path = tmp_path / "synthetic"
    path.write_bytes(b"a" * (256 * 1024))
    identity = file_identity(path)
    real_read = os.read
    sizes = []

    def read_then_grow(fd, size):
        chunk = real_read(fd, size)
        sizes.append(len(chunk))
        with path.open("ab") as handle:
            handle.write(b"b" * (256 * 1024))
        return chunk

    monkeypatch.setattr(os, "read", read_then_grow)
    started = time.monotonic()
    with pytest.raises(OSError):
        read_digest(path, identity)
    assert sum(sizes) <= identity[0]
    assert len(sizes) == 1
    assert time.monotonic() - started < 5


def test_fifo_is_rejected_without_reading_or_blocking(tmp_path):
    path = tmp_path / "synthetic-fifo"
    os.mkfifo(path)
    with patch("src.file_evidence.os.read", side_effect=AssertionError("unexpected read")):
        with pytest.raises(OSError):
            read_digest(path, file_identity(path))


def test_inventory_retains_all_flags_and_never_relists_after_mutation(tmp_path, monkeypatch):
    cur = tmp_path / "INBOX" / "cur"
    cur.mkdir(parents=True)
    trashed = cur / "one:2,T"
    live = cur / "one:2,FS"
    trashed.write_bytes(b"synthetic")
    live.write_bytes(b"synthetic")
    original = Path.iterdir
    calls = []

    def listed(directory):
        calls.append(directory)
        return original(directory)

    monkeypatch.setattr(Path, "iterdir", listed)
    inventory = FolderInventory()
    assert inventory.candidates(trashed) == sorted([trashed, live])
    assert inventory.candidates(trashed) == sorted([trashed, live])
    (cur / "two:2,S").write_bytes(b"synthetic mutation")
    assert inventory.candidates(trashed) is None
    assert calls == [cur]


def test_unreadable_directory_is_not_treated_as_absent(tmp_path, monkeypatch):
    inventory = FolderInventory()
    monkeypatch.setattr(
        inventory, "_signature", lambda _path: (_ for _ in ()).throw(PermissionError())
    )
    assert inventory.candidates(tmp_path / "INBOX/cur/one:2,S") is None
