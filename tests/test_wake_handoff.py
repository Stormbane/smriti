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
    assert f"(full letter: {path})" in out


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
