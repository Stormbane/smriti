"""The installer's privacy check names the remote the backup really pushes to.

``narada/.smriti/backup.py`` pushes the current branch to its upstream
(``origin`` when it has none, nothing on a detached HEAD). It is a
standalone script, so ``smriti.install.privacy.backup_remote`` is a copy of
its rule; these tests hold the two together on several repositories, check
that a push lands exactly where the installer said it would, and that an
answer about one remote isn't reused for another.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from smriti.install import core, privacy

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_SCRIPT = Path(__file__).resolve().parents[1] / "narada" / ".smriti" / "backup.py"
ORIGIN = "git@example.com:sam/ari-memory.git"
MIRROR = "https://example.com/sam/ari-public.git"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def backup() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smriti_backup_script", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(core, "HOME", home)
    monkeypatch.setattr(privacy, "git_crypt_exe", lambda: None)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(tmp_path: Path, *, commit: bool = True) -> Path:
    repo = tmp_path / "tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    if commit:
        _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _track(repo: Path, remote: str, branch: str = "main") -> None:
    """Make ``main`` track ``<remote>/<branch>``, as `git push -u` or `branch -u` would."""
    _git(repo, "config", "branch.main.remote", remote)
    _git(repo, "config", "branch.main.merge", f"refs/heads/{branch}")


def _bare(tmp_path: Path, name: str) -> str:
    path = tmp_path / f"{name}.git"
    _git(tmp_path, "init", "-q", "--bare", str(path))
    return str(path)


# Each builds a repository and returns it with the bare remotes it has, by URL.
Setup = Callable[[Path], tuple[Path, list[str]]]


def _upstream_on_origin(tmp_path: Path) -> tuple[Path, list[str]]:
    repo, origin = _repo(tmp_path), _bare(tmp_path, "origin")
    _git(repo, "remote", "add", "origin", origin)
    _track(repo, "origin")
    return repo, [origin]


def _upstream_on_another_remote(tmp_path: Path) -> tuple[Path, list[str]]:
    repo, origin, mirror = _repo(tmp_path), _bare(tmp_path, "origin"), _bare(tmp_path, "mirror")
    _git(repo, "remote", "add", "origin", origin)
    _git(repo, "remote", "add", "mirror", mirror)
    _track(repo, "mirror")
    return repo, [origin, mirror]


def _upstream_with_another_name(tmp_path: Path) -> tuple[Path, list[str]]:
    repo, origin = _repo(tmp_path), _bare(tmp_path, "origin")
    _git(repo, "remote", "add", "origin", origin)
    _track(repo, "origin", "trunk")
    return repo, [origin]


def _no_upstream(tmp_path: Path) -> tuple[Path, list[str]]:
    repo, origin, mirror = _repo(tmp_path), _bare(tmp_path, "origin"), _bare(tmp_path, "mirror")
    _git(repo, "remote", "add", "origin", origin)
    _git(repo, "remote", "add", "mirror", mirror)
    return repo, [origin, mirror]


def _upstream_names_a_url(tmp_path: Path) -> tuple[Path, list[str]]:
    repo, origin, direct = _repo(tmp_path), _bare(tmp_path, "origin"), _bare(tmp_path, "direct")
    _git(repo, "remote", "add", "origin", origin)
    _track(repo, direct)
    return repo, [origin, direct]


def _upstream_names_a_rewritten_url(tmp_path: Path) -> tuple[Path, list[str]]:
    """The upstream names .../private/memory.git; a pushInsteadOf sends pushes to public/."""
    repo, origin = _repo(tmp_path), _bare(tmp_path, "origin")
    urls = []
    for side in ("private", "public"):
        (tmp_path / side).mkdir()
        _git(tmp_path, "init", "-q", "--bare", str(tmp_path / side / "memory.git"))
        urls.append((tmp_path / side / "memory.git").as_posix())
    _git(repo, "remote", "add", "origin", origin)
    _track(repo, urls[0])
    _git(repo, "config", f"url.{(tmp_path / 'public').as_posix()}/.pushInsteadOf",
         f"{(tmp_path / 'private').as_posix()}/")
    return repo, [origin, *urls]


def _no_remote(tmp_path: Path) -> tuple[Path, list[str]]:
    return _repo(tmp_path), []


def _unborn_branch(tmp_path: Path) -> tuple[Path, list[str]]:
    repo, origin = _repo(tmp_path, commit=False), _bare(tmp_path, "origin")
    _git(repo, "remote", "add", "origin", origin)
    return repo, [origin]


def _detached(tmp_path: Path) -> tuple[Path, list[str]]:
    repo, remotes = _upstream_on_origin(tmp_path)
    _git(repo, "checkout", "-q", "--detach")
    return repo, remotes


SETUPS: dict[str, Setup] = {
    "upstream on origin": _upstream_on_origin,
    "upstream on another remote": _upstream_on_another_remote,
    "upstream with another name": _upstream_with_another_name,
    "no upstream": _no_upstream,
    "upstream names a URL": _upstream_names_a_url,
    "upstream names a rewritten URL": _upstream_names_a_rewritten_url,
    "no remote": _no_remote,
    "unborn branch": _unborn_branch,
    "detached HEAD": _detached,
}


def _script_remote(backup: ModuleType, repo: Path) -> str | None:
    target = backup.push_target(repo)
    if isinstance(target, str):
        return None  # pushes nothing
    args: list[str] = target[0]
    return args[-2]  # push [-q] [-u] <remote> <refspec>


@pytest.mark.parametrize("setup", list(SETUPS), ids=list(SETUPS))
def test_the_installer_and_the_backup_agree_on_the_remote(
        backup: ModuleType, tmp_path: Path, setup: str) -> None:
    repo, _ = SETUPS[setup](tmp_path)
    assert privacy.backup_remote(repo)[0] == _script_remote(backup, repo)


@pytest.mark.parametrize("setup", ["upstream on another remote", "no upstream",
                                   "upstream names a URL", "upstream names a rewritten URL",
                                   "detached HEAD"])
def test_the_backup_pushes_exactly_where_the_installer_says(tmp_path: Path, setup: str) -> None:
    repo, remotes = SETUPS[setup](tmp_path)
    (repo / "note.md").write_text("hello\n", encoding="utf-8")
    said = {os.path.normcase(str(Path(u).resolve())) for u in privacy.inspect_git(repo).remote_urls}

    env = {**os.environ, "SMRITI_ROOT": str(repo)}
    env.pop("SMRITI_INTERNAL", None)
    subprocess.run([sys.executable, str(_SCRIPT), "--tag", "t", "--foreground", "--push"],
                   env=env, check=True, timeout=120)

    head = _git(repo, "rev-parse", "HEAD")
    received = {os.path.normcase(str(Path(u).resolve())) for u in remotes
                if head in _git(Path(u), "for-each-ref", "--format=%(objectname)")}
    assert received == said


# ── What the installer asks and keeps ──────────────────────────────


def _decide(root: Path, **kwargs: object) -> privacy.PrivacyPlan:
    kwargs.setdefault("capture", False)
    kwargs.setdefault("remote_private", None)
    kwargs.setdefault("interactive", False)
    plan = privacy.decide(root, **kwargs)  # type: ignore[arg-type]
    privacy.apply(root, plan, now=NOW)
    return plan


def _fake_remotes(tmp_path: Path) -> Path:
    repo = _repo(tmp_path, commit=False)
    _git(repo, "remote", "add", "origin", ORIGIN)
    _git(repo, "remote", "add", "mirror", MIRROR)
    return repo


def test_a_branch_tracking_another_remote_is_inspected_there(tmp_path: Path) -> None:
    repo = _fake_remotes(tmp_path)
    _track(repo, "mirror")
    state = privacy.inspect_git(repo)
    assert state.remote == "mirror" and state.remote_urls == (MIRROR,)

    asked: list[str] = []

    def read(prompt: str) -> str:
        asked.append(prompt)
        return "n"

    plan = _decide(repo, interactive=True, read=read)
    assert asked == [f"{privacy.REMOTE_QUESTION.format(url=MIRROR)} [y/n] "]
    assert plan.remote == privacy.Choice(False, privacy.PROMPT)
    text = privacy.summary(repo, plan)
    assert f"to {MIRROR}, in plaintext" in text and ORIGIN not in text
    assert "remote remove mirror" in text
    record = json.loads(privacy.record_path(repo).read_text(encoding="utf-8"))
    assert record["remote"]["name"] == "mirror" and record["remote"]["urls"] == [MIRROR]


def test_an_answer_about_origin_is_not_kept_for_another_upstream(tmp_path: Path) -> None:
    repo = _fake_remotes(tmp_path)
    _decide(repo, remote_private=True)  # about origin
    assert _decide(repo).remote == privacy.Choice(True, privacy.KEPT)

    _track(repo, "mirror")
    assert _decide(repo).remote == privacy.Choice(None, privacy.DEFAULT)


def test_the_same_urls_under_another_remote_are_asked_again(tmp_path: Path) -> None:
    repo = _fake_remotes(tmp_path)
    _git(repo, "remote", "set-url", "mirror", ORIGIN)  # same URL, another remote
    _decide(repo, remote_private=True)
    _track(repo, "mirror")
    assert _decide(repo).remote == privacy.Choice(None, privacy.DEFAULT)


def test_an_answer_recorded_before_names_were_kept_still_covers_origin(tmp_path: Path) -> None:
    repo = _fake_remotes(tmp_path)
    path = privacy.record_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"remote": {"urls": [ORIGIN], "private": True, "how": "flag",
                                           "at": "2026-11-01T00:00:00+00:00"}}),
                    encoding="utf-8")
    assert _decide(repo).remote == privacy.Choice(True, privacy.KEPT)
    _track(repo, "mirror")
    assert _decide(repo).remote == privacy.Choice(None, privacy.DEFAULT)


def test_a_detached_head_pushes_nothing_and_says_so(tmp_path: Path) -> None:
    repo = _fake_remotes(tmp_path)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    _git(repo, "checkout", "-q", "--detach")
    state = privacy.inspect_git(repo)
    assert state.remote_urls == () and "detached" in state.no_push
    plan = _decide(repo, remote_private=True)
    assert plan.remote == privacy.Choice(None, privacy.SKIPPED)
    assert "pushed:    nothing; HEAD is detached" in privacy.summary(repo, plan)


# ── git's URL rewrites ──────────────────────────────────────────────

PRIVATE = "https://example.com/private/memory.git"
PUBLIC = "https://example.com/public/memory.git"


def _rewrite(repo: Path, kind: str, base: str, prefix: str) -> None:
    _git(repo, "config", "--add", f"url.{base}.{kind}", prefix)


def _direct_upstream(tmp_path: Path, url: str = PRIVATE) -> Path:
    repo = _fake_remotes(tmp_path)
    _track(repo, url)
    return repo


def test_push_instead_of_wins_over_instead_of(tmp_path: Path) -> None:
    repo = _direct_upstream(tmp_path)
    # insteadOf has the longer prefix, and still pushInsteadOf decides the push.
    _rewrite(repo, "insteadOf", "https://mirror.example.com/private/", "https://example.com/private/")
    _rewrite(repo, "pushInsteadOf", "https://push.example.com/", "https://example.com/")
    state = privacy.inspect_git(repo)
    assert state.remote == PRIVATE  # what the branch names
    assert state.remote_urls == ("https://push.example.com/private/memory.git",)  # git's push


@pytest.mark.parametrize("kind", ["insteadOf", "pushInsteadOf"])
def test_the_longest_matching_prefix_wins(tmp_path: Path, kind: str) -> None:
    repo = _direct_upstream(tmp_path)
    _rewrite(repo, kind, "https://short.example.com/", "https://example.com/")
    _rewrite(repo, kind, "https://long.example.com/", "https://example.com/private/")
    _rewrite(repo, kind, "https://shortest.example.com/", "https:")
    assert privacy.inspect_git(repo).remote_urls == ("https://long.example.com/memory.git",)


def test_instead_of_alone_rewrites_the_push(tmp_path: Path) -> None:
    repo = _direct_upstream(tmp_path)
    _rewrite(repo, "insteadOf", "https://example.com/public/", "https://example.com/private/")
    assert privacy.inspect_git(repo).remote_urls == (PUBLIC,)


def test_a_url_no_rewrite_matches_is_pushed_as_it_is(tmp_path: Path) -> None:
    repo = _direct_upstream(tmp_path)
    _rewrite(repo, "pushInsteadOf", "https://example.com/public/", "https://other.example.com/")
    assert privacy.inspect_git(repo).remote_urls == (PRIVATE,)


@pytest.mark.parametrize("kind", ["insteadOf", "pushInsteadOf"])
def test_a_named_remote_s_urls_come_rewritten_from_git(tmp_path: Path, kind: str) -> None:
    repo = _fake_remotes(tmp_path)
    _git(repo, "remote", "set-url", "mirror", PRIVATE)
    _track(repo, "mirror")
    _rewrite(repo, kind, "https://example.com/public/", "https://example.com/private/")
    state = privacy.inspect_git(repo)
    assert state.remote == "mirror" and state.remote_urls == (PUBLIC,)


def test_the_question_names_where_the_push_goes(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _direct_upstream(tmp_path)
    _rewrite(repo, "pushInsteadOf", "https://example.com/public/", "https://example.com/private/")
    asked: list[str] = []

    def read(prompt: str) -> str:
        asked.append(prompt)
        return "n"

    _decide(repo, interactive=True, read=read)
    assert asked == [f"{privacy.REMOTE_QUESTION.format(url=PUBLIC)} [y/n] "]
    out = " ".join(capsys.readouterr().out.split())
    assert f"to {PUBLIC} (the branch's upstream names {PRIVATE}" in out
    record = json.loads(privacy.record_path(repo).read_text(encoding="utf-8"))
    assert record["remote"]["urls"] == [PUBLIC]


def test_changing_a_rewrite_makes_the_saved_answer_stale(tmp_path: Path) -> None:
    repo = _direct_upstream(tmp_path)
    _rewrite(repo, "pushInsteadOf", "https://example.com/private2/", "https://example.com/private/")
    _decide(repo, remote_private=True)
    assert _decide(repo).remote == privacy.Choice(True, privacy.KEPT)

    _git(repo, "config", "--unset-all", "url.https://example.com/private2/.pushInsteadOf")
    _rewrite(repo, "pushInsteadOf", "https://example.com/public/", "https://example.com/private/")
    plan = _decide(repo)
    assert plan.remote == privacy.Choice(None, privacy.DEFAULT)
    assert f"to {PUBLIC}, in plaintext" in privacy.summary(repo, plan)


# ── What the summary tells you to do ───────────────────────────────


def _steps(text: str) -> str:
    return text[text.index("To keep the tree unreadable"):]


def test_the_remedy_reaches_origin_too_when_the_upstream_is_elsewhere(tmp_path: Path) -> None:
    # The upstream's remote and origin are the same public repository: unsetting
    # the upstream, or removing its remote, sends the backup to origin.
    repo = _fake_remotes(tmp_path)
    for name in ("origin", "mirror"):
        _git(repo, "remote", "set-url", name, PUBLIC)
    _track(repo, "mirror")
    steps = _steps(privacy.summary(repo, _decide(repo)))
    assert f'git -C "{repo}" remote remove mirror' in steps
    assert f'git -C "{repo}" remote remove origin' in steps
    flat = " ".join(steps.split())
    assert "alone only sends the backup to origin" in flat
    assert "every remote it can push to: mirror, the branch's upstream remote, and origin" in flat


def test_the_remedy_for_an_upstream_naming_a_url_also_reaches_origin(tmp_path: Path) -> None:
    repo = _direct_upstream(tmp_path, PUBLIC)
    _git(repo, "remote", "set-url", "origin", PUBLIC)
    steps = _steps(privacy.summary(repo, _decide(repo)))
    assert f'git -C "{repo}" branch --unset-upstream' in steps
    assert f'git -C "{repo}" remote remove origin' in steps
    assert "alone only sends the backup to origin" in " ".join(steps.split())


def test_a_remote_named_with_a_slash_is_a_remote_not_a_url(tmp_path: Path) -> None:
    # git allows a slash in a remote's name; the advice is to remove that remote.
    repo = _fake_remotes(tmp_path)
    _git(repo, "remote", "rename", "mirror", "team/backup")
    _track(repo, "team/backup")
    git = privacy.inspect_git(repo)
    assert git.remote == "team/backup" and git.remote_urls == (MIRROR,)
    explanation = " ".join(privacy.remote_explanation(repo, git).split())
    assert f"push the memory tree to team/backup, {MIRROR}." in explanation
    assert "names team/backup" not in explanation
    steps = _steps(privacy.summary(repo, _decide(repo)))
    assert f'git -C "{repo}" remote remove team/backup' in steps
    assert "unset-upstream" not in steps


def test_the_remedy_when_origin_is_the_destination(tmp_path: Path) -> None:
    repo = _fake_remotes(tmp_path)
    steps = _steps(privacy.summary(repo, _decide(repo)))
    assert f'git -C "{repo}" remote remove origin' in steps
    assert "remote remove mirror" not in steps and "unset-upstream" not in steps
    assert "git-crypt init" in steps
