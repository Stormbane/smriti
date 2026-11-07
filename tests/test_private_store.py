"""The private store keeps its key out of git and never loses a write."""
from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from smriti.private.store import PrivateStore  # noqa: E402


def _store(tmp_path: Path) -> PrivateStore:
    store = PrivateStore(tmp_path)
    store.init()
    return store


def test_init_keeps_the_key_out_of_the_committed_contract(tmp_path: Path) -> None:
    _store(tmp_path)
    private = tmp_path / "private"
    key = (private / ".key").read_text(encoding="ascii").strip()
    assert len(key) == 44  # a Fernet key
    assert key not in (private / "README.md").read_text(encoding="utf-8")
    assert ".key" in (private / ".gitignore").read_text(encoding="utf-8").split()


def test_init_is_idempotent_and_never_replaces_the_key(tmp_path: Path) -> None:
    _store(tmp_path)
    key = (tmp_path / "private" / ".key").read_text(encoding="ascii")
    _store(tmp_path)
    assert (tmp_path / "private" / ".key").read_text(encoding="ascii") == key


def test_existing_ignore_rules_are_kept(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()
    (private / ".gitignore").write_text("*.plaintext", encoding="utf-8")
    _store(tmp_path)
    rules = (private / ".gitignore").read_text(encoding="utf-8").split()
    assert "*.plaintext" in rules and ".key" in rules


def test_a_restored_store_without_its_key_never_gets_a_new_one(tmp_path: Path) -> None:
    """After a restore from git, ciphertext exists but .key doesn't (it's never committed)."""
    store = _store(tmp_path)
    store.write("before the restore", entity="Ari", session_id="s1")
    (tmp_path / "private" / ".key").unlink()  # what a fresh clone looks like

    PrivateStore(tmp_path).init()  # e.g. any CLI command at startup
    assert not (tmp_path / "private" / ".key").exists(), "a second key would mix the store"
    with pytest.raises(RuntimeError, match="restore .key"):
        PrivateStore(tmp_path).write("after", entity="Ari", session_id="s1")


def test_write_and_consented_read_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.write("a private thought", entity="Ari", session_id="s1")
    assert b"a private thought" not in path.read_bytes()
    consent = store.issue_consent(session_id="s1", reason="test")
    assert store.read_with_consent(path, consent) == "a private thought"


# ---------------------------------------------------------------- an older install's key


def _legacy(tmp_path: Path, block: str = "", newline: str = "\n") -> tuple[Path, str]:
    """A store as older installs left it: the key in a code block of the committed README."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    private = tmp_path / "private"
    private.mkdir()
    readme = "\n".join(["# Contract", "", "Sam's commitment to Ari.", "", "## The key", "",
                        "```", f"key: {key}", *([block] if block else []), "```", "",
                        "## Forward path", "", "Phase 2.", ""])
    (private / "README.md").write_bytes(readme.replace("\n", newline).encode("utf-8"))
    return private, key


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_legacy_key_moves_to_dot_key_and_out_of_the_readme(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, newline: str) -> None:
    """Outside review #6: the key was still read from the committed README, so it
    stayed there, and in every later commit of it."""
    from cryptography.fernet import Fernet

    from smriti.private.store import LEGACY_KEY_NOTE

    private, key = _legacy(tmp_path, newline=newline)
    old = Fernet(key.encode("ascii")).encrypt(b"written by the old install")
    (private / "2026").mkdir()
    (private / "2026" / "2026-04-12-001.md.enc").write_bytes(old)

    store = PrivateStore(tmp_path)
    store.init()

    assert (private / ".key").read_text(encoding="ascii").strip() == key  # the same key
    readme = (private / "README.md").read_bytes().decode("utf-8")
    assert key not in readme
    expected = "\n".join(["# Contract", "", "Sam's commitment to Ari.", "", "## The key", "",
                          "", "## Forward path", "", "Phase 2.", ""])
    assert readme == expected.replace("\n", newline)  # the rest kept, line endings too
    # Plainly told: the key is in git history, and how to rotate it.
    warned = caplog.text
    assert "git history" in warned and "rotate" in warned and "docs/PRIVACY.md" in warned
    assert "git history" in (private / LEGACY_KEY_NOTE).read_text(encoding="utf-8")
    # Nothing is lost: the old files decrypt, and new ones use the same key.
    consent = store.issue_consent(session_id="s1", reason="test")
    assert store.read_with_consent(private / "2026" / "2026-04-12-001.md.enc",
                                   consent) == "written by the old install"
    path = store.write("after the move", entity="Ari", session_id="s1")
    assert Fernet(key.encode("ascii")).decrypt(path.read_bytes()) == b"after the move"


def test_the_move_happens_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    private, _key = _legacy(tmp_path)
    PrivateStore(tmp_path).init()
    readme = (private / "README.md").read_bytes()
    caplog.clear()
    PrivateStore(tmp_path).init()
    assert (private / "README.md").read_bytes() == readme
    assert "git history" not in caplog.text


def test_a_block_with_more_in_it_loses_only_the_key_line(tmp_path: Path) -> None:
    private, key = _legacy(tmp_path, block="created: 2026-04-12")
    PrivateStore(tmp_path).init()
    readme = (private / "README.md").read_text(encoding="utf-8")
    assert key not in readme and "```\ncreated: 2026-04-12\n```" in readme


def test_a_write_without_init_moves_the_key_too(tmp_path: Path) -> None:
    private, key = _legacy(tmp_path)
    PrivateStore(tmp_path).write("first", entity="Ari", session_id="s1")
    assert (private / ".key").read_text(encoding="ascii").strip() == key
    assert key not in (private / "README.md").read_text(encoding="utf-8")


def test_a_dot_key_with_another_key_wins_and_the_readme_is_left(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from cryptography.fernet import Fernet

    private, key = _legacy(tmp_path)
    other = Fernet.generate_key().decode("ascii")
    (private / ".key").write_text(other + "\n", encoding="ascii")
    store = PrivateStore(tmp_path)
    store.init()
    assert key in (private / "README.md").read_text(encoding="utf-8")
    assert "isn't the one in private/.key" in caplog.text
    path = store.write("x", entity="Ari", session_id="s1")
    assert Fernet(other.encode("ascii")).decrypt(path.read_bytes()) == b"x"


_MIGRATED = "\n".join(["# Contract", "", "Sam's commitment to Ari.", "", "## The key", "", "",
                       "## Forward path", "", "Phase 2.", ""])


def test_two_migrations_at_once_take_out_the_key_and_nothing_else(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex review of H13.9: a second command that found the key's line before the first
    moved it deleted whatever line had taken that place in the shorter README."""
    private, key = _legacy(tmp_path)
    first, second = PrivateStore(tmp_path), PrivateStore(tmp_path)
    real = PrivateStore._legacy_key_line
    interleaved: list[bool] = []

    def found_then_overtaken(self: PrivateStore) -> tuple[int, str] | None:
        found = real(self)
        if self is second and not interleaved:
            interleaved.append(True)
            first._migrate_legacy_key()  # the other command finishes in between
        return found

    monkeypatch.setattr(PrivateStore, "_legacy_key_line", found_then_overtaken)
    second._migrate_legacy_key()
    assert interleaved
    assert (private / "README.md").read_text(encoding="utf-8") == _MIGRATED
    assert (private / ".key").read_text(encoding="ascii").strip() == key


def test_the_migration_holds_the_readmes_lock(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.core.files import FileBusy, edit_lock

    private, _key = _legacy(tmp_path)
    store = PrivateStore(tmp_path)
    real = store._remove_readme_line
    held: list[bool] = []

    def removing(*args: object) -> None:
        try:
            with edit_lock(private / "README.md", wait_s=0.05):
                held.append(False)
        except FileBusy:
            held.append(True)  # another command waits here, then finds the key gone
        real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_remove_readme_line", removing)
    store.init()
    assert held == [True]


def test_a_migration_cut_short_after_the_key_is_finished_by_the_next(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """`.key` written, the README not yet cleaned: the same key in both. The next run
    takes it out of the README (leaving it would keep the key in every commit)."""
    from smriti.private.store import LEGACY_KEY_NOTE

    private, key = _legacy(tmp_path)
    (private / ".gitignore").write_text(".key\n", encoding="utf-8")
    (private / ".key").write_text(key + "\n", encoding="ascii")
    PrivateStore(tmp_path).init()
    assert (private / "README.md").read_text(encoding="utf-8") == _MIGRATED
    assert (private / LEGACY_KEY_NOTE).exists() and "git history" in caplog.text


@pytest.mark.parametrize("first_use", ["write", "read"])
def test_the_ignore_rule_is_in_place_before_a_moved_key_is_written(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_use: str) -> None:
    """Codex review of H13.9: a write or read without init moved the key to .key with no
    private/.gitignore, so the next backup could commit it."""
    import os

    from cryptography.fernet import Fernet

    private, key = _legacy(tmp_path)
    old = private / "2026" / "2026-04-12-001.md.enc"
    old.parent.mkdir()
    old.write_bytes(Fernet(key.encode("ascii")).encrypt(b"from the old install"))
    ignore = private / ".gitignore"
    real_open = os.open
    ignored_when_written: list[bool] = []

    def watching(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(str(path)).name == ".key" and flags & os.O_CREAT:
            ignored_when_written.append(
                ignore.exists() and ".key" in ignore.read_text(encoding="utf-8").split())
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", watching)
    store = PrivateStore(tmp_path)  # no init: the first use moves the key
    if first_use == "write":
        store.write("new", entity="Ari", session_id="s1")
    else:
        consent = store.issue_consent(session_id="s1", reason="test")
        assert store.read_with_consent(old, consent) == "from the old install"
    monkeypatch.undo()
    assert ignored_when_written == [True]
    if shutil.which("git") is not None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        done = subprocess.run(["git", "check-ignore", "-q", "private/.key"], cwd=tmp_path)
        assert done.returncode == 0, "git would commit the moved key"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; Windows relies on ACLs")
def test_the_moved_key_is_the_owners_alone(tmp_path: Path) -> None:
    private, _key = _legacy(tmp_path)
    PrivateStore(tmp_path).init()
    assert stat.S_IMODE((private / ".key").stat().st_mode) == 0o600


def test_smriti_status_keeps_saying_so_until_the_note_is_deleted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    from smriti import cli
    from smriti.private.store import LEGACY_KEY_NOTE

    monkeypatch.setenv("SMRITI_ROOT", str(tmp_path))
    private, _key = _legacy(tmp_path)
    assert cli.main(["status"]) == 0  # its start moves the key
    assert "Private:    WARNING:" in capsys.readouterr().out
    assert cli.main(["status"]) == 0
    assert "git history" in capsys.readouterr().out  # every time, not only the first
    (private / LEGACY_KEY_NOTE).unlink()  # rotated
    assert cli.main(["status"]) == 0
    assert "Private:" not in capsys.readouterr().out


def test_concurrent_writes_never_overwrite_each_other(tmp_path: Path) -> None:
    store = _store(tmp_path)
    paths: list[Path] = []
    lock = threading.Lock()

    def write(i: int) -> None:
        p = store.write(f"note {i}", entity="Ari", session_id="s1")
        with lock:
            paths.append(p)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(paths)) == 16
    consent = store.issue_consent(session_id="s1", reason="test")
    texts = {store.read_with_consent(p, consent) for p in paths}
    assert texts == {f"note {i}" for i in range(16)}
