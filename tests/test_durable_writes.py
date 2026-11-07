"""Memory text and the morning's ledger reach the disk; index and state files don't wait for it.

Hardening H14.6 (both reviewers, outside review #7; Suti's decision on the
cost): ``write_atomic`` and ``append_memory`` never fsynced, so a power cut
could leave a journal, a todo list or the handoff empty. A durable write
fsyncs the temp file before ``os.replace``, and on POSIX the directory after
(best effort); an append fsyncs before its lock is let go. memwrite's writes
are durable by default; the ``index.md`` listings, the nightly's status and
marker, and the runtime and capture state aren't.

``os.fsync`` is replaced by a recorder, so these tests count and order the
flushes without depending on the disk.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from smriti.core import files
from smriti.core.files import create_new, sync_dir, write_atomic
from smriti.core.memwrite import append_memory, create_memory, write_memory


class Flushes:
    """What was fsynced, in order: "file" or "dir" per call, and the replaces between."""

    def __init__(self) -> None:
        self.events: list[str] = []

    @property
    def files(self) -> int:
        return self.events.count("file")

    @property
    def dirs(self) -> int:
        return self.events.count("dir")

    def clear(self) -> None:
        self.events.clear()


@pytest.fixture
def flushes(monkeypatch: pytest.MonkeyPatch) -> Flushes:
    seen = Flushes()
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        seen.events.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    def replace(src: object, dst: object) -> None:
        seen.events.append("replace")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    return seen


POSIX = sys.platform != "win32"
DIR_SYNCS = 1 if POSIX else 0  # Windows has no directory handle to flush


# ------------------------------------------------------------ the primitives


def test_a_durable_replace_flushes_the_file_before_the_rename_and_the_directory_after(
        tmp_path: Path, flushes: Flushes) -> None:
    path = tmp_path / "todo.md"
    write_atomic(path, "# Todo\n- [ ] Sam's list\n", durable=True)
    assert flushes.events == ["file", "replace"] + ["dir"] * DIR_SYNCS
    write_atomic(path, b"bytes too\n", durable=True)
    assert flushes.files == 2
    assert path.read_bytes() == b"bytes too\n"


def test_a_plain_replace_flushes_nothing(tmp_path: Path, flushes: Flushes) -> None:
    write_atomic(tmp_path / "state.json", "{}")
    write_atomic(tmp_path / "state.json", b"{}")
    assert flushes.events == ["replace", "replace"]


def test_a_durable_create_flushes_the_file_and_its_directory(
        tmp_path: Path, flushes: Flushes) -> None:
    first = create_new(tmp_path / "finding.md", "one", durable=True)
    second = create_new(tmp_path / "finding.md", "two", durable=True)
    assert (first.name, second.name) == ("finding.md", "finding-2.md")
    assert flushes.events == (["file"] + ["dir"] * DIR_SYNCS) * 2
    flushes.clear()
    create_new(tmp_path / "finding.md", "three")
    assert flushes.events == []


@pytest.mark.skipif(not POSIX, reason="Windows has no directory handle to flush")
def test_sync_dir_flushes_the_directory_on_posix(tmp_path: Path, flushes: Flushes) -> None:
    sync_dir(tmp_path)
    assert flushes.events == ["dir"]
    sync_dir(tmp_path / "missing")  # best effort: nothing to open, nothing raised
    assert flushes.events == ["dir"]


def test_sync_dir_does_nothing_on_windows(
        tmp_path: Path, flushes: Flushes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files.sys, "platform", "win32")
    sync_dir(tmp_path)
    assert flushes.events == []


# ---------------------------------------------------------------- memwrite


def test_memory_writes_are_durable_by_default(tmp_path: Path, flushes: Flushes) -> None:
    write_memory(tmp_path / "handoff-pending.md", "# Handoff\nAri: finish the tests.\n")
    assert flushes.files == 1
    flushes.clear()
    create_memory(tmp_path / "decisions" / "2026-11-08-tests.md", "# Decision\n")
    create_memory(tmp_path / "features" / "spec.md", "# Spec\n", unique=False)
    assert flushes.files == 2 and flushes.dirs == 2 * DIR_SYNCS
    flushes.clear()
    journal = tmp_path / "journal" / "11-08.md"
    append_memory(journal, "Sam asked for fsync.\n", header="# 11-08\n\n")
    assert flushes.events == ["file"] + ["dir"] * DIR_SYNCS  # a new file: its name too
    flushes.clear()
    append_memory(journal, "Ari measured it.\n", separator="\n---\n\n")
    assert flushes.events == ["file"]  # an existing file: the directory is as it was
    assert journal.read_text(encoding="utf-8").endswith("Ari measured it.\n")


def test_a_memory_write_can_opt_out(tmp_path: Path, flushes: Flushes) -> None:
    write_memory(tmp_path / "index.md", "# Index\n", durable=False)
    create_memory(tmp_path / "a.md", "a", durable=False)
    create_memory(tmp_path / "b.md", "b", unique=False, durable=False)
    append_memory(tmp_path / "c.md", "c", durable=False)
    assert "file" not in flushes.events and "dir" not in flushes.events


def test_an_entry_is_flushed_and_the_index_listing_above_it_is_not(flushes: Flushes) -> None:
    """``smriti_write`` appends its entry (durable), then regenerates the index.md listings
    above it (rebuilt by the next write in each directory: not flushed)."""
    from smriti.core.tree import tree_root
    from smriti.store.index_pages import update_index_pages

    root = tree_root()
    notes = root / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "index.md").write_text("# Notes\n", encoding="utf-8")
    entry = notes / "durability.md"
    append_memory(entry, "# Durability\nSam: the cost is accepted.\n")
    flushes.clear()
    assert update_index_pages(entry, root) == [notes / "index.md"]
    assert flushes.events == ["replace"]


# ---------------------------------------------------------- the morning's ledger


def test_every_write_of_the_morning_ledger_is_durable(tmp_path: Path, flushes: Flushes) -> None:
    from smriti.daylog import morning

    ledger = tmp_path / "log" / ".morning-sent" / "2026-11-08"
    claim = morning._claim_day(ledger)
    assert isinstance(claim, morning._Claim)
    assert flushes.events == ["file"] + ["dir"] * DIR_SYNCS  # the exclusive create
    flushes.clear()
    assert morning._commit(claim, {"chars": 42})  # "sending", before the send
    assert flushes.events == ["file", "replace"] + ["dir"] * DIR_SYNCS
    flushes.clear()
    morning._settle(claim, "sent")
    assert flushes.events == ["file", "replace"] + ["dir"] * DIR_SYNCS
    assert json.loads(ledger.read_text(encoding="utf-8"))["state"] == "sent"


def test_a_stale_claim_taken_over_is_durable(
        tmp_path: Path, flushes: Flushes, monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.daylog import morning

    ledger = tmp_path / ".morning-sent" / "2026-11-08"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"state": "claimed", "token": "x", "claimed_at": 0}),
                      encoding="utf-8")
    assert isinstance(morning._claim_day(ledger), morning._Claim)
    assert flushes.events == ["file", "replace"] + ["dir"] * DIR_SYNCS


# ------------------------------------------------------- what stays plain


def test_the_nightlys_status_and_scan_marker_are_not_flushed(
        tmp_path: Path, flushes: Flushes) -> None:
    from datetime import date

    from smriti.daylog import nightly

    nightly._write_status(tmp_path / "log" / ".nightly-status.json", {"ok": True})
    nightly._save_marker(tmp_path / ".smriti" / "nightly-scan.json",
                         nightly._Marker(date(2026, 11, 8), {}))
    assert flushes.events == ["replace", "replace"]
