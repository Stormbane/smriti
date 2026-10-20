"""Tests for the witness: stale-lock recovery, stamping, history checks."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentimestamps")

from opentimestamps.calendar import RemoteCalendar  # noqa: E402
from opentimestamps.core.notary import PendingAttestation  # noqa: E402
from opentimestamps.core.timestamp import Timestamp  # noqa: E402

from smriti import witness  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "master")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "journal.md").write_text("first night\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "snapshot: test")
    return tmp_path


@pytest.fixture
def calendars(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Calendars that accept every submission with a pending attestation."""
    submitted: list[str] = []

    def submit(self: Any, digest: bytes, timeout: Any = None) -> Timestamp:
        submitted.append(self.url)
        stamp = Timestamp(digest)
        stamp.attestations.add(PendingAttestation("https://alice.btc.calendar.opentimestamps.org"))
        return stamp

    monkeypatch.setattr(RemoteCalendar, "submit", submit)
    monkeypatch.setattr(RemoteCalendar, "get_timestamp",
                        lambda self, msg, timeout=None: (_ for _ in ()).throw(KeyError(msg)))
    return submitted


def test_stale_lock_is_cleared_fresh_lock_is_not(repo: Path) -> None:
    lock = repo / ".git" / "index.lock"
    lock.touch()
    assert witness.clear_stale_lock(repo) is False
    assert lock.exists()
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert witness.clear_stale_lock(repo) is True
    assert not lock.exists()


def test_stamp_writes_manifest_and_proof(repo: Path, calendars: list[str]) -> None:
    result = witness.run_witness(repo, "2026-10-20", push=False)
    assert result["ok"] is True
    assert result["calendars"] == len(witness.CALENDARS)
    manifest = repo / "witness" / "2026" / "2026-10-20.txt"
    assert witness.read_manifest(manifest)["head"] == _git(repo, "rev-parse", "HEAD")
    assert manifest.with_name("2026-10-20.txt.ots").exists()


def test_same_night_is_stamped_once(repo: Path, calendars: list[str]) -> None:
    witness.run_witness(repo, "2026-10-20", push=False)
    again = witness.run_witness(repo, "2026-10-20", push=False)
    assert again["calendars"] == -1
    assert len(calendars) == len(witness.CALENDARS)


def test_too_few_calendars_fails_visibly(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(self: Any, digest: bytes, timeout: Any = None) -> Timestamp:
        raise OSError("calendar unreachable")

    monkeypatch.setattr(RemoteCalendar, "submit", down)
    result = witness.run_witness(repo, "2026-10-20", push=False)
    assert result["ok"] is False
    assert "calendar" in str(result["error"])


def test_verify_passes_then_catches_rewritten_history(repo: Path, calendars: list[str]) -> None:
    witness.run_witness(repo, "2026-10-20", push=False)
    [check] = witness.verify(repo, offline=True)
    assert check.ok and check.in_history and check.digest_ok

    # Rewrite the night's memory after it was witnessed.
    (repo / "journal.md").write_text("a different first night\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-a", "--amend", "-m", "snapshot: test")
    [check] = witness.verify(repo, offline=True)
    assert not check.in_history
    assert any("rewritten" in p for p in check.problems)


def test_verify_catches_edited_manifest(repo: Path, calendars: list[str]) -> None:
    witness.run_witness(repo, "2026-10-20", push=False)
    manifest = repo / "witness" / "2026" / "2026-10-20.txt"
    manifest.write_bytes(manifest.read_bytes().replace(b"night: 2026-10-20", b"night: 2026-10-19"))
    [check] = witness.verify(repo, offline=True)
    assert not check.digest_ok
    assert not check.ok


def test_line_endings_do_not_break_a_proof(repo: Path, calendars: list[str]) -> None:
    witness.run_witness(repo, "2026-10-20", push=False)
    manifest = repo / "witness" / "2026" / "2026-10-20.txt"
    manifest.write_bytes(manifest.read_bytes().replace(b"\n", b"\r\n"))
    [check] = witness.verify(repo, offline=True)
    assert check.digest_ok
