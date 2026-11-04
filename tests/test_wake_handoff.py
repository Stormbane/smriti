"""A handoff written with smriti_write_handoff is shown at the next wake, once."""
from __future__ import annotations

from pathlib import Path

import pytest

from smriti.wake.briefing import HANDOFF_CAP, briefing, consume_handoff, pending_handoff

LETTER = "---\ntype: handoff\n---\n\n# Handoff to next session\n\nFinish the intentions slice.\n"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / ".smriti").mkdir(parents=True)
    (root / ".smriti" / "wake-context.md").write_text("# Ari\nTest identity.\n", encoding="utf-8")
    return root


def _write(root: Path, text: str = LETTER) -> Path:
    path = root / ".smriti" / "handoff-pending.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_briefing_shows_the_letter_near_the_top_without_consuming_it(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _write(root)
    out = briefing(root, cwd=tmp_path)
    assert "--- HANDOFF from your last session" in out
    assert "Finish the intentions slice." in out
    assert out.index("HANDOFF") < out.index("IDENTITY") if "IDENTITY" in out else True
    assert path.exists(), "rendering alone must not consume the letter"


def test_no_letter_no_section(tmp_path: Path) -> None:
    assert "HANDOFF" not in briefing(_root(tmp_path), cwd=tmp_path)


def test_long_letters_are_capped_with_a_pointer(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _write(root, LETTER + "x" * (HANDOFF_CAP * 2))
    out = briefing(root, cwd=tmp_path)
    from smriti.wake.briefing import handoff_archive_path

    archived = handoff_archive_path(root, path.read_text(encoding="utf-8"))
    assert f"(full letter: {archived}" in out
    shown = pending_handoff(root)
    assert shown is not None and consume_handoff(root, shown[1]) == archived
    assert archived.exists(), "the pointer must name where the letter now lives"


def test_consume_archives_the_shown_letter(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = _write(root)
    shown = pending_handoff(root)
    assert shown is not None
    archived = consume_handoff(root, shown[1])
    assert archived is not None and archived.read_text(encoding="utf-8") == LETTER
    assert archived.parent.name == "handoff-consumed"
    assert not path.exists()
    assert pending_handoff(root) is None


def test_a_letter_rewritten_after_rendering_is_kept(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(root)
    shown = pending_handoff(root)
    assert shown is not None
    _write(root, LETTER + "\nA newer note.\n")
    assert consume_handoff(root, shown[1]) is None
    assert pending_handoff(root) is not None


def test_session_start_consumes_after_printing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    from smriti.integrations.common import wake_runner

    root = _root(tmp_path)
    _write(root)
    monkeypatch.setenv("SMRITI_WAKE", "1")
    monkeypatch.chdir(tmp_path)
    assert wake_runner.run(entity_root=root, framing="raw") == 0
    assert "Finish the intentions slice." in capsys.readouterr().out
    assert pending_handoff(root) is None
    assert len(list((root / ".smriti" / "handoff-consumed").glob("*.md"))) == 1


def test_a_held_lock_leaves_the_letter_for_the_next_wake(tmp_path: Path) -> None:
    from smriti.wake.briefing import handoff_lock

    root = _root(tmp_path)
    _write(root)
    shown = pending_handoff(root)
    assert shown is not None
    with handoff_lock(root) as held:
        assert held
        assert consume_handoff(root, shown[1]) is None  # waits, then gives up
    assert pending_handoff(root) is not None
    assert consume_handoff(root, shown[1]) is not None  # free again


def test_a_crashed_holder_cannot_leave_the_lock_stuck(tmp_path: Path) -> None:
    """The OS releases the lock when its owner exits, however it exits."""
    import subprocess
    import sys

    root = _root(tmp_path)
    _write(root)
    code = "\n".join([
        "import importlib, os, sys",
        "from pathlib import Path",
        "b = importlib.import_module('smriti.wake.briefing')",
        "with b.handoff_lock(Path(sys.argv[1])) as held:",
        "    assert held",
        "    os._exit(0)  # die holding it, no cleanup",
    ])
    subprocess.run([sys.executable, "-c", code, str(root)], check=True, timeout=60)
    shown = pending_handoff(root)
    assert shown is not None and consume_handoff(root, shown[1]) is not None


def test_writer_and_wake_never_archive_an_unshown_letter(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent writes and wakes: everything archived is exactly what was shown."""
    import threading

    from smriti.mcp_server import handle_write_handoff

    root = _root(tmp_path)
    monkeypatch.setenv("SMRITI_ROOT", str(root))
    shown_texts: set[str] = set()
    archived: list[Path] = []

    def writer() -> None:
        for i in range(25):
            handle_write_handoff({"content": f"letter {i}"})

    def wake() -> None:
        for _ in range(25):
            pending = pending_handoff(root)
            if pending is None:
                continue
            shown_texts.add(pending[1])
            target = consume_handoff(root, pending[1])
            if target is not None:
                archived.append(target)

    threads = [threading.Thread(target=writer), threading.Thread(target=wake)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for target in archived:
        assert target.read_text(encoding="utf-8") in shown_texts
    assert not list((root / ".smriti").glob(".handoff-*.tmp"))
