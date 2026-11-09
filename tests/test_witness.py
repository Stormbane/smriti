"""Tests for the witness: stale-lock recovery, stamping, history checks."""

from __future__ import annotations

import http.client
import os
import subprocess
import time
import urllib.error
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


def test_stale_lock_is_cleared_fresh_lock_is_not(repo: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    import smriti.core.gitproc as gitproc

    monkeypatch.setattr(gitproc, "git_running", lambda: False)
    lock = repo / ".git" / "index.lock"
    lock.touch()
    assert witness.clear_stale_lock(repo) is False
    assert lock.exists()
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert witness.clear_stale_lock(repo) is True
    assert not lock.exists()


def test_old_lock_is_kept_while_git_runs(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Age isn't proof: a slow live git may hold an old-looking lock."""
    import smriti.core.gitproc as gitproc

    monkeypatch.setattr(gitproc, "git_running", lambda: True)
    lock = repo / ".git" / "index.lock"
    lock.touch()
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert witness.clear_stale_lock(repo) is False
    assert lock.exists()


def test_lock_replaced_after_inspection_is_kept(repo: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """A live git that takes the lock between our look and our delete keeps it."""
    import smriti.core.gitproc as gitproc

    lock = repo / ".git" / "index.lock"
    lock.touch()
    old = time.time() - 3600
    os.utime(lock, (old, old))

    def a_git_starts() -> bool:
        lock.write_text("live", encoding="utf-8")  # new content, new mtime
        return False

    monkeypatch.setattr(gitproc, "git_running", a_git_starts)
    assert witness.clear_stale_lock(repo) is False
    assert lock.read_text(encoding="utf-8") == "live"


def test_stamp_writes_manifest_and_proof(repo: Path, calendars: list[str]) -> None:
    night = _git(repo, "rev-parse", "HEAD")
    result = witness.run_witness(repo, "2026-10-20", push=False)
    assert result["ok"] is True
    assert result["calendars"] == len(witness.CALENDARS)
    manifest = repo / "witness" / "2026" / "2026-10-20.txt"
    assert witness.read_manifest(manifest)["head"] == night
    assert manifest.with_name("2026-10-20.txt.ots").exists()
    # The step's backup then commits the manifest and its proof.
    assert _git(repo, "log", "-1", "--format=%s") == "snapshot: witness"
    assert _git(repo, "status", "--porcelain") == ""


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


def test_verify_passes_then_catches_rewritten_history(
        repo: Path, calendars: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    # No backup commit on top of the night's, so amending HEAD rewrites the night.
    monkeypatch.setattr(witness, "_commit", lambda root, tag, push=False: {})
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


class _Reply:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_Reply":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def _explorers(monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]) -> list[str]:
    """``urlopen`` answering each explorer's requests from ``answers``: an exception
    to raise, or a block's body; returns the URLs asked, in order."""
    asked: list[str] = []

    def urlopen(url: str, timeout: float) -> _Reply:
        asked.append(url)
        base = next(b for b in witness._EXPLORERS if url.startswith(b))
        answer = answers[base]
        if isinstance(answer, BaseException):
            raise answer
        return _Reply(b"00000000abc" if "/block-height/" in url else answer)

    monkeypatch.setattr(witness.urllib.request, "urlopen", urlopen)
    return asked


@pytest.mark.parametrize("failure", [
    OSError("connection refused"),
    TimeoutError("timed out"),
    urllib.error.URLError("no route"),
    http.client.IncompleteRead(b""),
    b"<html>rate limited</html>",          # not JSON
    b'{"timestamp": 1}',                    # no merkle root
    b'["merkle_root"]',                     # not an object
])
def test_an_explorer_that_fails_hands_over_to_the_next(
        monkeypatch: pytest.MonkeyPatch, failure: Any) -> None:
    """H12.7: the handler is narrowed to what a request and its JSON can raise."""
    first, second = witness._EXPLORERS
    asked = _explorers(monkeypatch, {
        first: failure, second: b'{"merkle_root": "ff00", "timestamp": 1700000000}'})
    assert witness._block(800_000) == ("ff00", 1_700_000_000)
    assert asked[-1].startswith(second)


def test_no_explorer_answering_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _explorers(monkeypatch, {base: OSError("down") for base in witness._EXPLORERS})
    assert witness._block(800_000) is None


def test_a_bug_is_not_taken_for_an_explorer_down(monkeypatch: pytest.MonkeyPatch) -> None:
    _explorers(monkeypatch, {base: RuntimeError("a bug") for base in witness._EXPLORERS})
    with pytest.raises(RuntimeError, match="a bug"):
        witness._block(800_000)
