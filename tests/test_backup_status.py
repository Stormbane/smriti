"""H12.13: a failed backup commit or push is visible, not only in the backup log.

``smriti status`` and the nightly's status report the last commit and push
from ``<git dir>/smriti-backup.jsonl``, and since when pushes fail; the
nightly's witness step records the push it triggered. The script itself
still exits 0 whatever happens.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smriti import backup
from smriti.backup import backup_status, trigger

_SCRIPT = Path(__file__).resolve().parents[1] / "narada" / ".smriti" / "backup.py"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _tree(tmp_path: Path, remote: str | None = None) -> Path:
    """A memory tree in a git repository, with the backup script deployed as the installer does."""
    root = tmp_path / "tree"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.email", "ari@example.com")
    _git(root, "config", "user.name", "Ari")
    (root / ".smriti").mkdir()
    shutil.copy2(_SCRIPT, root / ".smriti" / "backup.py")
    (root / ".gitignore").write_text(".smriti/\n", encoding="utf-8")
    if remote is not None:
        _git(root, "remote", "add", "origin", remote)
    return root


def _log(root: Path, *records: dict[str, object]) -> None:
    """Write the backup log as the script writes it."""
    root.joinpath(".git").mkdir(parents=True, exist_ok=True)
    with (root / ".git" / "smriti-backup.jsonl").open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def _repo(root: Path) -> None:
    """A git repository at *root* (an existing ``.git`` with the backup log in it is kept)."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")


def _config_remote(root: Path, name: str = "origin") -> None:
    """A repository with a remote *name*, so git, as the status asks it, knows the remote."""
    _repo(root)
    _git(root, "remote", "add", name, f"/nowhere/{name}.git")


@pytest.fixture(autouse=True)
def _no_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMRITI_INTERNAL", raising=False)
    monkeypatch.delenv("SMRITI_BACKUP_RUN", raising=False)


# ---------------------------------------------------------------- an unreachable remote


@needs_git
def test_an_unreachable_remote_makes_status_say_the_push_failed_and_since_when(
        tmp_path: Path) -> None:
    root = _tree(tmp_path, remote=str(tmp_path / "gone" / "memory.git"))
    (root / "note.md").write_text("a note\n", encoding="utf-8")
    run = trigger("hourly", push=True, root=root)
    assert run.ran and run.commit == "ok" and run.push == "failed"
    assert run.detail  # git's reason, in a line

    status = backup_status(root)
    assert status.repo and not status.ok
    assert status.commit is not None and status.commit.state == "ok"
    assert status.push is not None and status.push.state == "failed"
    assert status.failing_since == status.push.at
    line = status.describe(timezone.utc)
    when = datetime.fromisoformat(status.push.at).strftime("%Y-%m-%d %H:%M")
    assert f"push FAILED since {when}" in line and status.push.detail in line

    # Still failing the next hour, with something new: the same "since".
    first = status.failing_since
    (root / "note.md").write_text("a second note\n", encoding="utf-8")
    trigger("hourly", push=True, root=root)
    again = backup_status(root)
    assert again.failing_since == first and again.push is not None
    assert again.push.state == "failed"


@needs_git
def test_the_script_still_exits_0_when_the_push_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, remote=str(tmp_path / "gone" / "memory.git"))
    (root / "note.md").write_text("a note\n", encoding="utf-8")
    env = {**os.environ, "SMRITI_ROOT": str(root)}
    done = subprocess.run([sys.executable, str(root / ".smriti" / "backup.py"), "--tag", "t",
                           "--foreground", "--push"], env=env, timeout=120)
    assert done.returncode == 0


@needs_git
def test_a_push_that_works_is_ok_and_the_run_says_so(tmp_path: Path) -> None:
    remote = tmp_path / "memory.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    root = _tree(tmp_path, remote=str(remote))
    (root / "note.md").write_text("a note\n", encoding="utf-8")
    run = trigger("hourly", push=True, root=root)
    assert (run.commit, run.push) == ("ok", "ok") and run.ok
    assert trigger("hourly", push=True, root=root).as_dict() == {
        "ok": True, "commit": "nothing to commit", "push": "up to date"}
    status = backup_status(root)
    assert status.ok and status.push is not None and status.push.state == "ok"
    assert status.failing_since == ""


@needs_git
def test_a_tree_with_no_remote_is_not_failing(tmp_path: Path) -> None:
    """No upstream and no origin: the script's push to origin fails, but there is
    nowhere to push, which isn't a failure of the backup (every hour, it would be)."""
    root = _tree(tmp_path)
    (root / "note.md").write_text("a note\n", encoding="utf-8")
    trigger("hourly", push=True, root=root)
    status = backup_status(root)
    assert status.ok
    assert status.push is not None and status.push.state == "skipped"
    assert "no remote 'origin' is configured" in status.describe()


# ---------------------------------------------------------------- reading the log


@needs_git
def test_since_when_is_the_first_failure_after_the_last_success(tmp_path: Path) -> None:
    root = tmp_path
    _log(root,
         {"at": "2026-11-04T01:17:00+00:00", "event": "push", "ok": False, "code": 128,
          "out": "fatal: an older failure", "to": "origin main"},
         {"at": "2026-11-04T02:17:00+00:00", "event": "commit", "tag": "hourly", "ok": True},
         {"at": "2026-11-04T02:17:05+00:00", "event": "push", "ok": True, "to": "origin main"},
         {"at": "2026-11-05T03:17:05+00:00", "event": "push", "ok": False, "code": 128,
          "out": "fatal: unable to access\nCould not resolve host", "to": "origin main"},
         {"at": "2026-11-05T04:17:00+00:00", "event": "commit", "tag": "hourly", "ok": True},
         {"at": "2026-11-06T04:17:05+00:00", "event": "push", "ok": False, "code": 128,
          "out": "fatal: unable to access\nCould not resolve host", "to": "origin main"})
    _config_remote(root)
    status = backup_status(root)
    assert status.failing_since == "2026-11-05T03:17:05+00:00"
    assert status.commit == backup.Outcome("ok", "2026-11-05T04:17:00+00:00")
    assert status.describe(timezone.utc) == (
        "commit ok 2026-11-05 04:17; push FAILED since 2026-11-05 03:17 "
        "(last tried 2026-11-06 04:17): fatal: unable to access Could not resolve host")
    assert status.as_dict()["failing_since"] == "2026-11-05T03:17:05+00:00"


@needs_git
def test_failures_before_a_success_are_not_this_streak(tmp_path: Path) -> None:
    """The scan goes on past the streak for an older commit; it must not extend the
    streak past the success that ended the one before (Codex review, P2)."""
    _log(tmp_path,
         {"at": "2026-11-01T01:17:00+00:00", "event": "commit", "tag": "hourly", "ok": True},
         {"at": "2026-11-02T01:17:05+00:00", "event": "push", "ok": False,
          "out": "fatal: no", "to": "origin main"},
         {"at": "2026-11-03T01:17:05+00:00", "event": "push", "ok": True, "to": "origin main"},
         {"at": "2026-11-04T01:17:05+00:00", "event": "push", "ok": False,
          "out": "fatal: no", "to": "origin main"})
    _config_remote(tmp_path)
    status = backup_status(tmp_path)
    assert status.failing_since == "2026-11-04T01:17:05+00:00"
    assert status.commit == backup.Outcome("ok", "2026-11-01T01:17:00+00:00")


@needs_git
def test_a_log_of_failures_only_says_since_its_first(tmp_path: Path) -> None:
    _log(tmp_path, *({"at": f"2026-11-0{d}T01:17:00+00:00", "event": "push", "ok": False,
                      "out": "fatal: no", "to": "origin main"} for d in (3, 4, 5)))
    _config_remote(tmp_path)
    assert backup_status(tmp_path).failing_since == "2026-11-03T01:17:00+00:00"


@needs_git
@pytest.mark.parametrize("where", ["an included file", "the global config",
                                   "the worktree config"])
def test_a_remote_defined_outside_the_repositorys_config_file_is_a_real_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    """Git's effective configuration decides whether the remote exists; reading only
    .git/config called these pushes' failures "no remote", and the night ok (Codex
    review, P2)."""
    root = tmp_path / "tree"
    _repo(root)
    url = (tmp_path / "gone" / "memory.git").as_posix()
    if where == "an included file":
        (root / ".git" / "remotes.config").write_text(
            f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")
        _git(root, "config", "include.path", "remotes.config")
    elif where == "the global config":
        glob = tmp_path / "global.gitconfig"
        glob.write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    else:
        _git(root, "config", "extensions.worktreeConfig", "true")
        _git(root, "config", "--worktree", "remote.origin.url", url)
    assert "[remote" not in (root / ".git" / "config").read_text(encoding="utf-8")
    _log(root, {"at": "2026-11-06T01:17:05+00:00", "event": "push", "ok": False,
                "out": "fatal: Could not read from remote repository.",
                "to": "origin refs/heads/main:refs/heads/main"})
    status = backup_status(root)
    assert status.push is not None and status.push.state == "failed" and not status.ok


@needs_git
def test_a_failed_push_to_a_path_with_a_space_is_a_failure(tmp_path: Path) -> None:
    """The push line's ``to`` is the remote, then the refspec; a path may hold a space."""
    _repo(tmp_path)
    _log(tmp_path, {"at": "2026-11-06T01:17:05+00:00", "event": "push", "ok": False,
                    "out": "fatal: no", "to": "/srv/my memory.git refs/heads/main:refs/heads/main"})
    assert backup._push_target({"to": "/srv/my memory.git refs/heads/main:refs/heads/main"}) == (
        "/srv/my memory.git")
    assert not backup_status(tmp_path).ok


def test_when_git_cannot_say_a_failed_push_stays_a_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp

    from smriti.install import privacy

    def broken(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git")

    _log(tmp_path, {"at": "2026-11-06T01:17:05+00:00", "event": "push", "ok": False,
                    "out": "fatal: no", "to": "origin refs/heads/main:refs/heads/main"})
    monkeypatch.setattr(privacy.subprocess, "run", broken)
    assert not backup_status(tmp_path).ok
    monkeypatch.setattr(privacy.subprocess, "run", lambda *a, **k: sp.CompletedProcess(
        a[0], 128, stdout="", stderr="fatal: not a git repository"))
    assert not backup_status(tmp_path).ok


def test_a_failed_commit_is_a_failure(tmp_path: Path) -> None:
    _log(tmp_path, {"at": "2026-11-06T01:17:00+00:00", "event": "add_failed", "tag": "hourly",
                    "code": 128, "out": "fatal: Unable to create '.git/index.lock'"})
    status = backup_status(tmp_path)
    assert not status.ok
    assert status.describe(timezone.utc) == (
        "commit FAILED 2026-11-06 01:17: fatal: Unable to create '.git/index.lock'; "
        "no push recorded")


@needs_git
def test_a_skipped_push_and_a_direct_url_are_told_apart(tmp_path: Path) -> None:
    _repo(tmp_path)
    _log(tmp_path, {"at": "2026-11-06T01:17:00+00:00", "event": "push_skipped", "tag": "hourly",
                    "reason": "detached HEAD: no branch to push"})
    status = backup_status(tmp_path)
    assert status.ok and "push skipped 2026-11-06 01:17: detached HEAD" in status.describe(
        timezone.utc)
    # An upstream that names a URL has somewhere to go: its failure is one.
    _log(tmp_path, {"at": "2026-11-06T02:17:00+00:00", "event": "push", "ok": False,
                    "out": "fatal: no", "to": "/srv/memory.git refs/heads/main:refs/heads/main"})
    assert not backup_status(tmp_path).ok


def test_no_repository_and_no_log(tmp_path: Path) -> None:
    assert backup_status(tmp_path).as_dict() == {"ok": True, "repo": False}
    (tmp_path / ".git").mkdir()
    assert backup_status(tmp_path).describe() == "no backup recorded yet"


def test_a_linked_worktree_reads_the_common_log(tmp_path: Path) -> None:
    common = tmp_path / "main" / ".git"
    worktree_git = common / "worktrees" / "tree"
    worktree_git.mkdir(parents=True)
    (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
    _log(tmp_path / "main", {"at": "2026-11-06T01:17:00+00:00", "event": "commit", "ok": True})
    assert backup.git_common_dir(tree) == common.resolve()
    assert backup_status(tree).commit is not None


def test_a_run_reads_its_own_lines_and_an_older_scripts(tmp_path: Path) -> None:
    root = tmp_path
    _log(root, {"at": "2026-11-06T01:00:00+00:00", "event": "commit", "tag": "hourly", "ok": True})
    path = root / ".git" / "smriti-backup.jsonl"
    start = path.stat().st_size
    _log(root,
         {"at": "2026-11-06T01:00:01+00:00", "event": "skipped", "tag": "hourly",
          "run": "other", "reason": "another backup is running"},
         {"at": "2026-11-06T01:00:02+00:00", "event": "commit", "tag": "witness",
          "run": "mine", "ok": True},
         {"at": "2026-11-06T01:00:03+00:00", "event": "push", "tag": "witness",
          "run": "mine", "ok": False, "out": "fatal: gone"})
    events = backup._events_of_run(path, "mine", start, "2026-11-06T01:00:00+00:00", "witness")
    assert backup._summarise(events, push=True) == backup.BackupRun(
        ran=True, commit="ok", push="failed", detail="fatal: gone")

    # An older script writes no run id: the lines since the start, others' tags left out.
    start = path.stat().st_size
    _log(root,
         {"at": "2026-11-06T02:00:01+00:00", "event": "skipped", "tag": "hourly",
          "reason": "another backup is running"},
         {"at": "2026-11-06T02:00:02+00:00", "event": "commit", "tag": "witness", "ok": True},
         {"at": "2026-11-06T02:00:03+00:00", "event": "push", "ok": True})
    events = backup._events_of_run(path, "new", start, "2026-11-06T02:00:00+00:00", "witness")
    assert backup._summarise(events, push=True).as_dict() == {
        "ok": True, "commit": "ok", "push": "ok"}


def test_a_run_skipped_for_another_backup_says_so() -> None:
    run = backup._summarise([{"event": "skipped", "tag": "witness",
                              "reason": "another backup is running"}], push=True)
    assert run.as_dict() == {"ok": True, "commit": "skipped", "push": "skipped",
                             "detail": "another backup is running"}


def _deploy(root: Path, body: str) -> None:
    """A stand-in for the deployed backup script."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".smriti").mkdir(exist_ok=True)
    (root / ".smriti" / "backup.py").write_text(body, encoding="utf-8")


def test_a_script_that_fails_before_logging_is_not_a_success(tmp_path: Path) -> None:
    """The script exits 0 whatever git does, so another code is the script failing; with
    no line logged, its run read as nothing to commit and up to date (Codex review, P2)."""
    _deploy(tmp_path, "raise RuntimeError('a broken deployment')\n")
    run = trigger("hourly", push=True, root=tmp_path)
    assert not run.ran and not run.ok
    assert run.detail == "the backup script exited 1"
    assert (run.commit, run.push) == ("", "")


def test_a_script_that_fails_after_its_commit_keeps_what_it_logged(tmp_path: Path) -> None:
    _deploy(tmp_path, (
        "import json, os, pathlib, sys\n"
        "log = pathlib.Path(os.environ['SMRITI_ROOT']) / '.git' / 'smriti-backup.jsonl'\n"
        "log.write_text(json.dumps({'at': '2026-11-06T01:17:00+00:00', 'event': 'commit',\n"
        "    'tag': 'hourly', 'ok': True, 'run': os.environ['SMRITI_BACKUP_RUN']}) + '\\n')\n"
        "sys.exit(2)\n"))
    run = trigger("hourly", push=True, root=tmp_path)
    assert not run.ok
    assert (run.commit, run.push, run.detail) == ("ok", "", "the backup script exited 2")


def test_a_script_still_running_at_the_timeout_is_not_a_success(tmp_path: Path) -> None:
    _deploy(tmp_path, "import time\ntime.sleep(3)\n")
    run = trigger("hourly", push=True, root=tmp_path, timeout=1)
    assert not run.ran and not run.ok
    assert run.detail == "still running after 1s; left to finish"


def test_trigger_says_why_it_did_not_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "no backup script" in trigger("t", root=tmp_path).detail
    (tmp_path / ".smriti").mkdir()
    (tmp_path / ".smriti" / "backup.py").write_text("", encoding="utf-8")
    assert trigger("t", root=tmp_path).detail == f"no git repository at {tmp_path}"
    monkeypatch.setenv("SMRITI_INTERNAL", "1")
    assert not trigger("t", root=tmp_path).ran


# ---------------------------------------------------------------- where it shows


def _failing(root: Path) -> None:
    _config_remote(root)
    _log(root, {"at": "2026-11-05T17:17:05+00:00", "event": "push", "ok": False,
                "out": "fatal: Could not read from remote repository.", "to": "origin main"})


@needs_git
def test_the_nightly_reports_the_backup_and_a_failing_push_fails_the_night(
        tmp_path: Path) -> None:
    from smriti.daylog.config import DaylogConfig
    from smriti.daylog.nightly import run_nightly
    from smriti.wake.briefing import _nightly_line

    cfg = DaylogConfig(root=tmp_path, sources=[])
    now = datetime(2026, 11, 5, 17, 30, tzinfo=timezone.utc)
    status = run_nightly(cfg, now=now, reindex=False, witness=False)
    assert status["steps"]["backup"] == {"ok": True, "repo": False}  # type: ignore[index]
    assert status["ok"] is True

    _failing(tmp_path)
    status = run_nightly(cfg, now=now, reindex=False, witness=False)
    step = status["steps"]["backup"]  # type: ignore[index]
    assert step["ok"] is False and step["push"]["state"] == "failed"  # type: ignore[index]
    assert step["failing_since"] == "2026-11-05T17:17:05+00:00"  # type: ignore[index]
    assert status["ok"] is False
    assert "NIGHTLY HAD FAILURES for 2026-11-05: backup" in _nightly_line(tmp_path)


@needs_git
def test_smriti_status_prints_the_backup_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    from smriti import cli

    monkeypatch.setenv("SMRITI_ROOT", str(tmp_path))
    monkeypatch.setenv("SMRITI_DAY_UTC_OFFSET", "10")
    _failing(tmp_path)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert ("Backup:     push FAILED since 2026-11-06 03:17: "
            "fatal: Could not read from remote repository.") in out


def test_smriti_backup_prints_what_the_run_did(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from smriti import cli

    monkeypatch.setattr(backup, "trigger", lambda tag, push=False: backup.BackupRun(
        ran=True, commit="ok", push="failed", detail="fatal: gone"))
    assert cli.main(["backup", "--push", "--tag", "hourly"]) == 0
    assert "Backup (hourly): commit ok; push failed: fatal: gone" in capsys.readouterr().out
    monkeypatch.setattr(backup, "trigger", lambda tag, push=False: backup.BackupRun(
        ran=False, commit="ok", detail="the backup script exited 2"))
    assert cli.main(["backup", "--push", "--tag", "hourly"]) == 0  # still best effort
    assert ("Backup (hourly): did not complete: the backup script exited 2 (commit ok)"
            in capsys.readouterr().out)


@needs_git
def test_the_witness_step_records_the_push_it_triggered(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("opentimestamps")
    from smriti import witness

    root = _tree(tmp_path, remote=str(tmp_path / "gone" / "memory.git"))
    (root / "note.md").write_text("a note\n", encoding="utf-8")
    monkeypatch.setattr(witness, "stamp", lambda path, **kw: 4)  # no calendars contacted
    result = witness.run_witness(root, "2026-11-05")
    push = result["push"]
    assert isinstance(push, dict)
    assert push["ok"] is False and push["push"] == "failed" and push["detail"]
