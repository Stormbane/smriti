"""Character practice must survive summarization and fit delivery budgets."""

from pathlib import Path

import pytest

from smriti.store import wake_summary
from smriti.wake import briefing


def test_character_survives_summary_and_refreshes(tmp_path: Path) -> None:
    character = tmp_path / wake_summary.CHARACTER_FILE
    character.parent.mkdir(parents=True)
    character.write_text("# Practice\nFirst practice.", encoding="utf-8")
    (tmp_path / "identity.md").write_text("Identity source.", encoding="utf-8")

    def executor(prompt: str) -> tuple[str, None]:
        assert "First practice." not in prompt
        return "# Identity\nA summary that omits the character.", None

    output = wake_summary.rebuild(tmp_path, executor_fn=executor)
    assert output is not None
    assert output.read_text(encoding="utf-8").startswith("# Practice\nFirst practice.")
    assert "First practice." in briefing(tmp_path, cwd=tmp_path)

    character.write_text("# Practice\nRevised practice.", encoding="utf-8")
    wake_summary.rebuild(tmp_path, executor_fn=executor)
    text = output.read_text(encoding="utf-8")
    assert "Revised practice." in text
    assert "First practice." not in text
    assert wake_summary.is_identity_file(wake_summary.CHARACTER_FILE)


def test_character_and_summary_share_existing_budget(tmp_path: Path) -> None:
    character = tmp_path / wake_summary.CHARACTER_FILE
    character.parent.mkdir(parents=True)
    practice = "x" * wake_summary.CHARACTER_CHAR_LIMIT
    character.write_text(practice, encoding="utf-8")
    (tmp_path / "identity.md").write_text("Identity source.", encoding="utf-8")
    output = wake_summary.rebuild(
        tmp_path, executor_fn=lambda _: ("summary\n" * 1000, None)
    )
    assert output is not None
    text = output.read_text(encoding="utf-8")
    assert text.startswith(practice)
    assert len(text) <= wake_summary.CONTEXT_CHAR_LIMIT


def test_oversized_character_keeps_previous_briefing(tmp_path: Path) -> None:
    character = tmp_path / wake_summary.CHARACTER_FILE
    character.parent.mkdir(parents=True)
    character.write_text("x" * (wake_summary.CHARACTER_CHAR_LIMIT + 1), encoding="utf-8")
    output = wake_summary.context_path(tmp_path)
    output.parent.mkdir()
    output.write_text("Previous valid briefing.", encoding="utf-8")
    assert wake_summary.rebuild(
        tmp_path, executor_fn=lambda _: pytest.fail("must not compose")
    ) is None
    assert output.read_text(encoding="utf-8") == "Previous valid briefing."
