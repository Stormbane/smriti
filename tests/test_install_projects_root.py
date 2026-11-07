"""H12.4: the projects root is recorded at install and reused, not hard-coded.

The installer linked project mirrors from ``C:/Projects``, one person's
machine, unless ``--projects-root`` said otherwise each time. Now the root
is recorded in ``<root>/.smriti/install.json``: a rerun links the same
projects, an install made before the record keeps the root its mirrors
point into, and a new install links none until it is told where they are.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from smriti.install import core

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(core, "HOME", home)
    # Whatever this machine has at C:/Projects, the tests choose.
    monkeypatch.setattr(core, "LEGACY_PROJECTS_ROOT", tmp_path / "no-such-dir")
    return home


@pytest.fixture()
def root(home: Path) -> Path:
    return home / ".ari"


def project(projects: Path, name: str) -> Path:
    (projects / name / ".ai" / "knowledge").mkdir(parents=True)
    return projects / name


def installer() -> ModuleType:
    """scripts/install.py, the checkout's way in, which runs smriti.install.cli."""
    path = REPO / "scripts" / "install.py"
    spec = importlib.util.spec_from_file_location("smriti_install_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install(root: Path, monkeypatch: pytest.MonkeyPatch, *flags: str) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    result = installer().main(["--harness", "none", "--memory-root", str(root),
                               "--skip-recall-daemon", "--no-capture", *flags])
    assert isinstance(result, int)
    return result


def recorded(root: Path) -> object:
    return core.read_install_record(root).get("projects_root")


def mirrors(root: Path) -> list[str]:
    path = root / "mirrors"
    return sorted(p.name for p in path.iterdir()) if path.is_dir() else []


def test_a_new_install_links_nothing_until_told(
        root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert install(root, monkeypatch) == 0

    assert mirrors(root) == []
    assert recorded(root) is None
    assert "pass --projects-root DIR to link them" in capsys.readouterr().out


def test_the_flag_is_recorded_and_a_rerun_links_the_same_projects(
        root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    projects = tmp_path / "code"
    project(projects, "kite")

    assert install(root, monkeypatch, "--projects-root", str(projects)) == 0
    assert recorded(root) == str(projects)
    assert mirrors(root) == ["kite"]
    record = core.read_install_record(root)
    from smriti import __version__
    assert record["recorded_by"] == __version__
    stamp = record["at"]

    project(projects, "lantern")
    assert install(root, monkeypatch) == 0  # no flag: the recorded root
    assert mirrors(root) == ["kite", "lantern"]
    assert core.read_install_record(root)["at"] == stamp  # unchanged, not rewritten


def test_a_new_flag_replaces_the_record(
        root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = tmp_path / "code", tmp_path / "work"
    project(first, "kite")
    project(second, "lantern")
    assert install(root, monkeypatch, "--projects-root", str(first)) == 0

    assert install(root, monkeypatch, "--projects-root", str(second)) == 0

    assert recorded(root) == str(second)
    assert mirrors(root) == ["kite", "lantern"]  # mirrors are never removed


def legacy_install(root: Path, projects: Path, names: list[str]) -> None:
    """An install made before the record: wake.py deployed, mirrors linked from *projects*."""
    (root / ".smriti").mkdir(parents=True)
    (root / ".smriti" / "wake.py").write_text('"""smriti wake"""\n', encoding="utf-8")
    for name in names:
        target = project(projects, name)
        assert core.make_junction(root / "mirrors" / name / "ai", target / ".ai") == "created"
        assert core.make_junction(root / "mirrors" / name / "knowledge",
                                  target / ".ai" / "knowledge") == "created"


def test_an_existing_install_keeps_the_root_its_mirrors_link_into(
        root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The reference install's case: mirrors made from the old default, no record."""
    projects = tmp_path / "Projects"
    legacy_install(root, projects, ["beacon", "kite"])
    project(projects, "lantern")  # appeared since the last install

    found = core.resolve_projects_root(root, None)
    assert (found.path, found.how) == (projects, "mirrors")

    assert install(root, monkeypatch) == 0
    assert recorded(root) == str(projects)
    assert mirrors(root) == ["beacon", "kite", "lantern"]
    assert f"projects root {projects}, where the existing mirrors link" in capsys.readouterr().out
    assert core.resolve_projects_root(root, None).how == "recorded"


def test_mirrors_that_disagree_decide_nothing(root: Path, tmp_path: Path) -> None:
    legacy_install(root, tmp_path / "a", ["kite"])
    target = project(tmp_path / "b", "lantern")
    core.make_junction(root / "mirrors" / "lantern" / "ai", target / ".ai")

    assert core.projects_root_from_mirrors(root) is None
    assert core.resolve_projects_root(root, None).how == "none"


def test_an_existing_install_without_mirrors_keeps_the_old_default_when_it_exists(
        root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (root / ".smriti").mkdir(parents=True)
    (root / ".smriti" / "daylog.json").write_text("{}", encoding="utf-8")
    old_default = tmp_path / "old-default"
    monkeypatch.setattr(core, "LEGACY_PROJECTS_ROOT", old_default)

    assert core.resolve_projects_root(root, None).how == "none"  # it doesn't exist here

    project(old_default, "kite")
    found = core.resolve_projects_root(root, None)
    assert (found.path, found.how) == (old_default, "legacy")
    assert install(root, monkeypatch) == 0
    assert recorded(root) == str(old_default)
    assert mirrors(root) == ["kite"]


def test_a_new_install_never_takes_the_old_default(
        root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_default = tmp_path / "old-default"
    project(old_default, "kite")
    monkeypatch.setattr(core, "LEGACY_PROJECTS_ROOT", old_default)

    assert core.resolve_projects_root(root, None).how == "none"


def test_a_recorded_root_that_is_gone_links_nothing(
        root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    gone = tmp_path / "gone"
    project(gone, "kite")
    assert install(root, monkeypatch, "--projects-root", str(gone)) == 0
    shutil.rmtree(gone)

    assert install(root, monkeypatch) == 0
    assert recorded(root) == str(gone)
    assert "0 project(s) with memory found under" in capsys.readouterr().out


def test_an_unreadable_record_is_none(root: Path) -> None:
    core.install_record_path(root).parent.mkdir(parents=True)
    core.install_record_path(root).write_text("{not json", encoding="utf-8")
    assert core.read_install_record(root) == {}
    core.install_record_path(root).write_text("[1]", encoding="utf-8")
    assert core.read_install_record(root) == {}


@pytest.mark.parametrize(("project_path", "name"), [
    ("C:/Projects/my-app", "C--Projects-my-app"),
    ("/home/sam/my.app", "-home-sam-my-app"),
    ("/home/sam/the_kite", "-home-sam-the-kite"),
])
def test_claude_codes_directory_is_named_after_the_project_path(
        tmp_path: Path, project_path: str, name: str) -> None:
    assert core.claude_project_dir(tmp_path, Path(project_path)) == tmp_path / "projects" / name


def test_a_project_with_only_claude_memory_is_found(tmp_path: Path) -> None:
    projects = tmp_path / "code"
    (projects / "kite").mkdir(parents=True)
    (projects / "notes-only").mkdir()
    claude = tmp_path / ".claude"
    (core.claude_project_dir(claude, projects / "kite") / "memory").mkdir(parents=True)

    assert core.discover_projects(projects, claude_dir=claude) == ["kite"]


def test_the_install_record_is_json(root: Path, tmp_path: Path) -> None:
    path = core.record_projects_root(root, tmp_path / "code")
    assert path == core.install_record_path(root)
    assert json.loads(path.read_text(encoding="utf-8"))["projects_root"] == str(tmp_path / "code")
    assert core.record_projects_root(root, tmp_path / "code") is None


def test_a_new_installs_choice_of_no_projects_is_kept(
        root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex review of H12.4 (P2): a new install without --projects-root linked
    nothing and recorded nothing, so its rerun found the wake.py it had deployed,
    took itself for an install from before the record, and linked C:/Projects
    unasked. The choice of none is recorded, and a rerun keeps it."""
    old_default = tmp_path / "old-default"
    project(old_default, "kite")
    monkeypatch.setattr(core, "LEGACY_PROJECTS_ROOT", old_default)

    assert install(root, monkeypatch) == 0
    assert mirrors(root) == []
    record = core.read_install_record(root)
    assert "projects_root" in record and record["projects_root"] is None

    assert install(root, monkeypatch) == 0  # now an existing install, with C:/Projects there
    assert mirrors(root) == []
    assert core.resolve_projects_root(root, None).how == "recorded"

    assert install(root, monkeypatch, "--projects-root", str(old_default)) == 0  # told: links
    assert mirrors(root) == ["kite"] and recorded(root) == str(old_default)


def test_only_a_record_without_the_key_predates_it(
        root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A record holding other keys but no projects root is an install from before
    the record: the old default still applies to it, and is then recorded."""
    old_default = tmp_path / "old-default"
    project(old_default, "kite")
    monkeypatch.setattr(core, "LEGACY_PROJECTS_ROOT", old_default)
    (root / ".smriti").mkdir(parents=True)
    (root / ".smriti" / "wake.py").write_text('"""smriti wake"""\n', encoding="utf-8")
    core.install_record_path(root).write_text('{"note": "kept"}', encoding="utf-8")

    found = core.resolve_projects_root(root, None)
    assert (found.path, found.how) == (old_default, "legacy")
    assert install(root, monkeypatch) == 0
    assert core.read_install_record(root)["note"] == "kept"
    assert recorded(root) == str(old_default)


def test_a_relative_root_with_dot_dot_is_normalised(
        root: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex review of H12.4 (P2): from C:\Projects\smriti, --projects-root ..
    stayed C:\Projects\smriti\.. once made absolute, so Claude Code's directory
    for kite was looked up as C--Projects-smriti----kite: a project with only
    Claude memory was missed, and others lost their auto-memory link."""
    projects = tmp_path / "code"
    (projects / "smriti").mkdir(parents=True)
    (projects / "kite").mkdir()  # no .ai: found only through its Claude memory
    claude_memory = core.claude_project_dir(home / ".claude", projects / "kite") / "memory"
    claude_memory.mkdir(parents=True)
    monkeypatch.chdir(projects / "smriti")

    assert core.resolve_projects_root(root, "..").path == projects
    assert install(root, monkeypatch, "--projects-root", "..") == 0

    assert recorded(root) == str(projects)
    assert mirrors(root) == ["kite"]
    assert core.is_junction(root / "mirrors" / "kite" / "auto-memory")


def test_claude_codes_directory_is_named_after_the_normalised_path(tmp_path: Path) -> None:
    assert (core.claude_project_dir(tmp_path, Path("/home/sam/code/smriti/../kite"))
            == core.claude_project_dir(tmp_path, Path("/home/sam/code/kite")))
