"""The private store keeps its key out of git and never loses a write."""
from __future__ import annotations

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


def test_a_legacy_readme_key_still_works(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    private = tmp_path / "private"
    private.mkdir()
    (private / "README.md").write_text(f"# Contract\n\n## The key\n\n```\nkey: {key}\n```\n",
                                       encoding="utf-8")
    store = PrivateStore(tmp_path)
    store.init()
    assert not (private / ".key").exists(), "a legacy key must not be replaced"
    path = store.write("old install", entity="Ari", session_id="s1")
    assert Fernet(key.encode("ascii")).decrypt(path.read_bytes()) == b"old install"


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
