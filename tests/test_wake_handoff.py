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

    archived = handoff_archive_path(root, path, path.read_text(encoding="utf-8"))
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


def test_a_newer_letter_claimed_by_mistake_is_never_lost(tmp_path: Path) -> None:
    """Rewritten after rendering, then rewritten again: the unseen one is archived, not dropped."""
    root = _root(tmp_path)
    _write(root)
    shown = pending_handoff(root)
    assert shown is not None
    _write(root, LETTER + "\nB\n")
    # Simulate an even newer letter landing between the claim and the give-back.
    import importlib

    b = importlib.import_module("smriti.wake.briefing")  # the package re-exports a same-named function

    real_read = b._read

    def read_then_newer(path: Path) -> str | None:
        text = real_read(path)
        if path.name.startswith(".handoff-claim-"):
            _write(root, LETTER + "\nC\n")
        return text

    b._read = read_then_newer
    try:
        assert consume_handoff(root, shown[1]) is None
    finally:
        b._read = real_read
    assert "C" in (pending_handoff(root) or ("", ""))[1]
    unseen = list((root / ".smriti" / "handoff-consumed").glob("*-unseen.md"))
    assert len(unseen) == 1 and "B" in unseen[0].read_text(encoding="utf-8")
