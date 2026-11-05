"""The memory root: a variable when set, else ~/.narada where it exists, else ~/.smriti.

Every test here runs against a temporary home with both variables unset,
so nothing reads or creates a real ``~/.narada`` or ``~/.smriti``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smriti.core.tree import smriti_db_path, tree_root, tree_root_source, tree_root_warning


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary home, with SMRITI_ROOT and NARADA_ROOT unset."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("SMRITI_ROOT", raising=False)
    monkeypatch.delenv("NARADA_ROOT", raising=False)
    assert Path.home() == home
    return home


def test_a_fresh_home_gets_the_neutral_default(home: Path) -> None:
    assert tree_root() == home / ".smriti"
    assert tree_root_source() == "default ~/.smriti"
    assert tree_root_warning() == ""
    assert smriti_db_path() == home / ".smriti" / ".smriti" / "index.db"
    assert not (home / ".smriti").exists()  # choosing a root creates nothing


def test_an_existing_narada_root_is_kept(home: Path) -> None:
    (home / ".narada").mkdir()
    assert tree_root() == home / ".narada"
    assert tree_root_source() == "legacy ~/.narada"
    assert tree_root_warning() == ""


def test_a_narada_file_is_not_a_root(home: Path) -> None:
    (home / ".narada").write_text("not a tree", encoding="utf-8")
    assert tree_root() == home / ".smriti"


def test_the_legacy_root_wins_over_a_new_one_with_a_warning(home: Path) -> None:
    """An install made before 0.2 never moves to a ~/.smriti that appears beside it."""
    (home / ".narada").mkdir()
    (home / ".smriti").mkdir()
    assert tree_root() == home / ".narada"
    assert tree_root_source() == "legacy ~/.narada"
    assert tree_root_warning() == (
        "both ~/.narada and ~/.smriti exist; using ~/.narada (set SMRITI_ROOT to choose)")


@pytest.mark.parametrize("variable", ["SMRITI_ROOT", "NARADA_ROOT"])
def test_a_variable_wins_over_both_and_silences_the_warning(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str,
) -> None:
    (home / ".narada").mkdir()
    (home / ".smriti").mkdir()
    chosen = tmp_path / "elsewhere"
    monkeypatch.setenv(variable, str(chosen))
    assert tree_root() == chosen.resolve()
    assert tree_root_source() == variable
    assert tree_root_warning() == ""


def test_smriti_root_wins_over_narada_root(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NARADA_ROOT", str(tmp_path / "old"))
    monkeypatch.setenv("SMRITI_ROOT", str(tmp_path / "new"))
    assert tree_root() == (tmp_path / "new").resolve()
    assert tree_root_source() == "SMRITI_ROOT"


def test_the_root_is_chosen_at_each_call(home: Path) -> None:
    assert tree_root() == home / ".smriti"
    (home / ".narada").mkdir()
    assert tree_root() == home / ".narada"


@pytest.mark.parametrize("dirs,line,warning", [
    ((), "(default ~/.smriti)", False),
    ((".narada",), "(legacy ~/.narada)", False),
    ((".narada", ".smriti"), "(legacy ~/.narada)", True),
])
def test_status_names_the_root_and_the_rule_that_chose_it(
    home: Path, capsys: pytest.CaptureFixture[str], dirs: tuple[str, ...], line: str,
    warning: bool,
) -> None:
    from smriti import cli

    for name in dirs:
        (home / name).mkdir()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"Tree root:  {tree_root()} {line}\n" in out
    assert ("Warning:    both ~/.narada and ~/.smriti exist" in out) is warning


def test_status_names_the_variable_that_chose_the_root(
    capsys: pytest.CaptureFixture[str], _isolated_memory_root: Path,
) -> None:
    from smriti import cli

    assert cli.main(["status"]) == 0
    assert f"Tree root:  {_isolated_memory_root.resolve()} (SMRITI_ROOT)\n" in (
        capsys.readouterr().out)


def test_the_installer_defaults_to_the_chosen_root(home: Path) -> None:
    from smriti.install.core import default_memory_root

    assert default_memory_root() == home / ".smriti"
    (home / ".narada").mkdir()
    assert default_memory_root() == home / ".narada"


def test_the_recall_hook_follows_the_chosen_root(home: Path) -> None:
    from smriti.recall.config import load_config
    from smriti.recall.hook import _build_query

    (home / ".narada").mkdir()
    assert load_config().log_path == home / ".narada" / ".smriti" / "recall.jsonl"
    inside = home / ".narada" / "notes" / "lantern.md"
    assert _build_query(str(inside)) is None  # never recall the tree into itself
    assert _build_query(str(home / "code" / "lantern.md")) == "lantern"
