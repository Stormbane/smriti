"""The installer's privacy questions (hardening H9.11).

Capture copies every conversation into the memory tree, and the backup
pushes the tree in plaintext unless git-crypt is set up, so the installer
makes the person choose. These pin the choice's effect on daylog.json,
the flags that answer it without a terminal, the safe default when nobody
can answer, the treatment of an existing install, and the summary.

Everything runs in temp directories with a temp HOME; no test touches a
real memory root or harness config.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from smriti.daylog.config import load_config
from smriti.install import core, privacy

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git is not installed")

REMOTE = "git@example.com:sam/ari-memory.git"
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)


# ── Helpers ─────────────────────────────────────────────────────────


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp HOME, so the installer's home-directory checks and defaults
    never reach the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(core, "HOME", home)
    return home


@pytest.fixture()
def root(home: Path) -> Path:
    return home / ".ari"


@pytest.fixture(autouse=True)
def _no_git_crypt_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """git-crypt is absent unless a test says otherwise, whatever this
    machine has on PATH."""
    monkeypatch.setattr(privacy, "git_crypt_exe", lambda: None)


def mark_log_for_git_crypt(root: Path) -> None:
    (root / ".gitattributes").write_text(
        "log/** filter=git-crypt diff=git-crypt\n", encoding="utf-8"
    )


def configure_git_crypt_filter(root: Path) -> None:
    """What `git-crypt init` or `git-crypt unlock` puts in a clone's config."""
    for key in ("clean", "smudge"):
        subprocess.run(["git", "-C", str(root), "config", f"filter.git-crypt.{key}",
                        f"git-crypt {key}"], check=True)


def fake_git_crypt(monkeypatch: pytest.MonkeyPatch, status: dict[str, bool] | None) -> None:
    """git-crypt on PATH, whose `status` reports ``status``."""
    monkeypatch.setattr(privacy, "git_crypt_exe", lambda: "git-crypt")
    monkeypatch.setattr(privacy, "git_crypt_status", lambda _root, _exe: status)


def write_daylog(root: Path, data: object) -> Path:
    path = privacy.daylog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return path


def read_daylog(root: Path) -> object:
    return json.loads(privacy.daylog_path(root).read_text(encoding="utf-8"))


def git_repo(root: Path, url: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if url is not None:
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", url], check=True)


def track(root: Path, rel: str, text: str = '{"who": "user", "text": "hi"}\n') -> None:
    """Write a file and stage it, as an earlier backup's `git add -A` would have."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--", rel], check=True)


def no_prompt(_question: str) -> str:
    raise AssertionError("asked a question it shouldn't have")


def answers(*replies: str) -> tuple[Callable[[str], str], list[str]]:
    """A fake input(): returns ``replies`` in order, records each prompt."""
    asked: list[str] = []
    queue = list(replies)

    def read(prompt: str) -> str:
        asked.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    return read, asked


def decide_and_apply(root: Path, **kwargs: object) -> privacy.PrivacyPlan:
    kwargs.setdefault("capture", None)
    kwargs.setdefault("remote_private", None)
    kwargs.setdefault("interactive", False)
    kwargs.setdefault("read", no_prompt)
    plan = privacy.decide(root, **kwargs)  # type: ignore[arg-type]
    privacy.apply(root, plan, now=NOW)
    return plan


# ── Capture: daylog.json ────────────────────────────────────────────


class TestCaptureChoice:
    def test_no_capture_turns_capture_off_and_keeps_other_keys(self, root: Path) -> None:
        before = {
            "user_name": "Sam",
            "agent_name": "Ari",
            "notify_cmd": ["notify", "--to", "sam"],
            "location": "Lisbon, Portugal",
            "extra_sources": [{"name": "notes", "kind": "voice_md", "glob": "/x/*.md"}],
        }
        write_daylog(root, before)

        plan = decide_and_apply(root, capture=False)

        assert plan.capture == privacy.Choice(False, privacy.FLAG)
        after = read_daylog(root)
        assert after == {**before, "capture": False, "sources": []}
        assert privacy.capture_sources(root) == []  # extra_sources kept, and off

    def test_no_capture_creates_daylog_when_missing(self, root: Path) -> None:
        decide_and_apply(root, capture=False)
        assert read_daylog(root) == {"capture": False, "sources": []}
        assert privacy.capture_sources(root) == []

    def test_capture_removes_empty_sources_and_keeps_other_keys(self, root: Path) -> None:
        write_daylog(root, {"sources": [], "profile": "x", "user_name": "Sam"})

        decide_and_apply(root, capture=True)

        assert read_daylog(root) == {"profile": "x", "user_name": "Sam", "capture": True}
        kinds = {spec.kind for spec in privacy.capture_sources(root)}
        assert kinds == {"claude_jsonl", "codex_rollout", "voice_md"}

    def test_capture_says_so_in_daylog_json(self, root: Path) -> None:
        """H11.1: capture is off unless daylog.json turns it on, so yes writes it there."""
        write_daylog(root, {"user_name": "Sam"})

        decide_and_apply(root, capture=True)

        assert read_daylog(root) == {"user_name": "Sam", "capture": True}
        assert {spec.name for spec in privacy.capture_sources(root)} == {
            "claude", "codex", "voice"}

    def test_no_capture_turns_capture_true_to_false(self, root: Path) -> None:
        write_daylog(root, {"capture": True, "user_name": "Sam"})

        decide_and_apply(root, capture=False)

        assert read_daylog(root) == {"capture": False, "user_name": "Sam", "sources": []}

    def test_capture_leaves_custom_sources_alone(self, root: Path) -> None:
        custom = {"sources": [{"name": "mine", "kind": "claude_jsonl", "glob": "/a/*.jsonl"}]}
        path = write_daylog(root, custom)
        before = path.read_bytes()

        decide_and_apply(root, capture=True)

        assert path.read_bytes() == before

    def test_a_flag_matching_the_file_rewrites_nothing(self, root: Path) -> None:
        path = write_daylog(root, {"capture": False, "sources": [], "user_name": "Sam"})
        before = path.read_bytes()

        decide_and_apply(root, capture=False)

        assert path.read_bytes() == before

    @pytest.mark.parametrize("before", [
        {},
        {"capture": True},
        {"sources": []},
        {"sources": [{"name": "mine", "kind": "claude_jsonl", "glob": "/a/*.jsonl"}]},
    ])
    def test_no_capture_captures_nothing_extra_sources_included(
            self, root: Path, before: dict[str, object]) -> None:
        """H15.3: "Capture nothing" left extra_sources captured. This replaces
        the test that pinned it (test_extra_sources_still_captured_after_no_capture)."""
        extra = [{"name": "phone", "kind": "voice_md", "glob": "/phone/*.md"}]
        write_daylog(root, {**before, "extra_sources": extra, "user_name": "Sam"})

        plan = decide_and_apply(root, capture=False)

        assert privacy.capture_sources(root) == []
        assert load_config(root).capture_off == '.smriti/daylog.json says "capture": false'
        assert "captured:  nothing" in privacy.summary(root, plan)
        after = read_daylog(root)
        assert after["extra_sources"] == extra and after["user_name"] == "Sam"

        # Turned back on, the default sources apply again, the extras with them.
        decide_and_apply(root, capture=True)

        names = {spec.name for spec in privacy.capture_sources(root)}
        assert names == {"claude", "codex", "voice", "phone"}
        assert read_daylog(root) == {"capture": True, "extra_sources": extra,
                                     "user_name": "Sam"}

    def test_capture_turns_an_explicit_false_back_on(self, root: Path) -> None:
        write_daylog(root, {"capture": False, "user_name": "Sam"})

        decide_and_apply(root, capture=True)

        assert read_daylog(root) == {"capture": True, "user_name": "Sam"}
        assert {spec.name for spec in privacy.capture_sources(root)} == {
            "claude", "codex", "voice"}

    def test_capture_beside_its_own_sources_drops_an_explicit_false(self, root: Path) -> None:
        mine = [{"name": "mine", "kind": "claude_jsonl", "glob": "/a/*.jsonl"}]
        write_daylog(root, {"capture": False, "sources": mine})

        decide_and_apply(root, capture=True)

        assert read_daylog(root) == {"sources": mine}
        assert [spec.name for spec in privacy.capture_sources(root)] == ["mine"]

    def test_malformed_daylog_is_refused_before_any_question(self, root: Path) -> None:
        path = privacy.daylog_path(root)
        path.parent.mkdir(parents=True)
        path.write_text('{"user_name": "Sam",', encoding="utf-8")

        with pytest.raises(privacy.PrivacyConfigError, match="isn't valid JSON"):
            privacy.decide(root, capture=False, remote_private=None,
                           interactive=True, read=no_prompt)
        assert path.read_text(encoding="utf-8") == '{"user_name": "Sam",'

    def test_a_json_array_is_refused(self, root: Path) -> None:
        write_daylog(root, [1, 2])
        with pytest.raises(privacy.PrivacyConfigError, match="isn't a JSON object"):
            privacy.decide(root, capture=None, remote_private=None, interactive=False)


class TestNoTerminal:
    def test_fresh_root_without_flag_captures_nothing(self, root: Path) -> None:
        plan = decide_and_apply(root)

        assert plan.capture == privacy.Choice(False, privacy.DEFAULT)
        assert read_daylog(root) == {"capture": False, "sources": []}
        text = privacy.summary(root, plan)
        assert "captured:  nothing" in text
        assert "capture is OFF: there was no terminal" in text
        assert "--capture" in text

    def test_existing_daylog_is_kept_byte_for_byte(self, root: Path) -> None:
        path = write_daylog(root, {"user_name": "Sam", "agent_name": "Ari", "profile": "x",
                                   "capture": True})
        before = path.read_bytes()

        plan = decide_and_apply(root)

        assert plan.capture == privacy.Choice(True, privacy.KEPT)
        assert path.read_bytes() == before
        assert "capture is on because .smriti/daylog.json says so" in privacy.summary(root, plan)

    def test_an_existing_daylog_that_never_chose_captures_nothing(self, root: Path) -> None:
        """H11.1: no "capture" and no "sources" was every transcript on the machine."""
        path = write_daylog(root, {"user_name": "Sam", "agent_name": "Ari", "profile": "x"})
        before = path.read_bytes()

        plan = decide_and_apply(root)

        assert plan.capture == privacy.Choice(False, privacy.KEPT)
        assert path.read_bytes() == before
        text = " ".join(privacy.summary(root, plan).split())
        assert ('captured: nothing (.smriti/daylog.json neither says "capture": true nor '
                'lists "sources")') in text
        assert "capture is OFF because .smriti/daylog.json neither says" in text
        assert "--capture" in text

    def test_existing_daylog_with_capture_off_stays_off(self, root: Path) -> None:
        path = write_daylog(root, {"sources": [], "user_name": "Sam"})
        before = path.read_bytes()

        plan = decide_and_apply(root)

        assert plan.capture == privacy.Choice(False, privacy.KEPT)
        assert path.read_bytes() == before

    def test_existing_install_without_daylog_captures_nothing(self, root: Path) -> None:
        """An install from before the question. Until H11.1 it captured every
        transcript on the machine; now it captures nothing until told to."""
        (root / ".smriti").mkdir(parents=True)
        (root / ".smriti" / "wake.py").write_text("# smriti wake\n", encoding="utf-8")

        plan = decide_and_apply(root)

        assert plan.capture == privacy.Choice(False, privacy.KEPT)
        assert not privacy.daylog_path(root).exists()
        text = privacy.summary(root, plan)
        assert "captured:  nothing (there is no .smriti/daylog.json)" in text
        assert "capture is OFF because there is no .smriti/daylog.json" in text

    def test_a_default_is_not_an_answer(self, root: Path) -> None:
        """The next run with a terminal still asks."""
        decide_and_apply(root)
        read, asked = answers("y")

        plan = decide_and_apply(root, interactive=True, read=read)

        assert plan.capture == privacy.Choice(True, privacy.PROMPT)
        assert len(asked) == 1
        assert "sources" not in read_daylog(root)  # type: ignore[operator]

    def test_is_interactive_is_false_without_a_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert privacy.is_interactive() is False
        monkeypatch.setattr(sys, "stdin", None)
        assert privacy.is_interactive() is False


class TestPrompts:
    def test_fresh_root_needs_an_explicit_answer(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        read, asked = answers("", "maybe", "n")

        plan = decide_and_apply(root, interactive=True, read=read)

        assert plan.capture == privacy.Choice(False, privacy.PROMPT)
        assert asked[0] == f"{privacy.CAPTURE_QUESTION} [y/n] "
        assert len(asked) == 3
        out = " ".join(capsys.readouterr().out.split())  # undo the wrapping
        assert "Claude Code transcripts, Codex rollouts and voice transcripts" in out
        assert f"into {root / 'log'}" in out
        assert "The backup commits them" in out and "pushes them" in out
        assert read_daylog(root) == {"capture": False, "sources": []}

    def test_existing_root_offers_its_current_state(self, root: Path) -> None:
        write_daylog(root, {"user_name": "Sam", "capture": True})
        read, asked = answers("")

        plan = decide_and_apply(root, interactive=True, read=read)

        assert asked == [f"{privacy.CAPTURE_QUESTION} [Y/n] "]
        assert plan.capture == privacy.Choice(True, privacy.PROMPT)
        assert read_daylog(root) == {"user_name": "Sam", "capture": True}

    def test_existing_root_that_never_chose_offers_off(self, root: Path) -> None:
        write_daylog(root, {"user_name": "Sam"})
        read, asked = answers("")

        plan = decide_and_apply(root, interactive=True, read=read)

        assert asked == [f"{privacy.CAPTURE_QUESTION} [y/N] "]
        assert plan.capture == privacy.Choice(False, privacy.PROMPT)
        assert read_daylog(root) == {"capture": False, "user_name": "Sam", "sources": []}

    def test_end_of_input_falls_back_to_the_safe_default(self, root: Path) -> None:
        read, _ = answers()
        plan = decide_and_apply(root, interactive=True, read=read)
        assert plan.capture == privacy.Choice(False, privacy.DEFAULT)

    def test_an_answer_is_recorded_and_not_asked_again(self, root: Path) -> None:
        read, _ = answers("n")
        decide_and_apply(root, interactive=True, read=read)

        record = json.loads(privacy.record_path(root).read_text(encoding="utf-8"))
        assert record["capture"] == {
            "enabled": False, "how": "prompt", "at": "2026-11-05T09:00:00+00:00",
        }

        plan = decide_and_apply(root, interactive=True, read=no_prompt)
        assert plan.capture == privacy.Choice(False, privacy.KEPT)

    def test_a_flag_overrides_a_recorded_answer(self, root: Path) -> None:
        read, _ = answers("n")
        decide_and_apply(root, interactive=True, read=read)

        decide_and_apply(root, capture=True, interactive=True, read=no_prompt)

        assert "sources" not in read_daylog(root)  # type: ignore[operator]


def record_a_yes(root: Path, daylog: dict[str, object] | None) -> dict[str, object]:
    """What a "yes" left behind before H11.1: the answer recorded, and daylog.json
    as it was (none, for a new root), since the defaults captured then."""
    if daylog is not None:
        write_daylog(root, daylog)
    entry = {"enabled": True, "how": "flag", "at": "2026-11-01T09:00:00+00:00"}
    path = privacy.record_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"capture": entry}), encoding="utf-8")
    return entry


class TestUpgrade:
    """H11.1 turned capture off unless daylog.json turns it on. A yes given
    before then left daylog.json to the defaults, so a rerun writes it out."""

    @pytest.mark.parametrize("daylog,after", [
        (None, {"capture": True}),
        ({"user_name": "Sam"}, {"user_name": "Sam", "capture": True}),
        ({"extra_sources": [{"name": "phone", "kind": "voice_md", "glob": "/p/*.md"}]},
         {"extra_sources": [{"name": "phone", "kind": "voice_md", "glob": "/p/*.md"}],
          "capture": True}),
    ])
    def test_a_recorded_yes_is_written_out(self, root: Path, daylog: dict[str, object] | None,
                                           after: dict[str, object]) -> None:
        entry = record_a_yes(root, daylog)
        assert {s.name for s in privacy.capture_sources(root)} <= {"phone"}  # off, or extras

        plan = decide_and_apply(root, interactive=True, read=no_prompt)

        assert plan.capture == privacy.Choice(True, privacy.RECORDED)
        assert read_daylog(root) == after
        assert {"claude", "codex", "voice"} <= {s.name for s in privacy.capture_sources(root)}
        record = json.loads(privacy.record_path(root).read_text(encoding="utf-8"))
        assert record["capture"] == entry  # the person's answer, as they gave it
        assert 'now says "capture": true' in privacy.summary(root, plan)
        # And the next run keeps it, asking nothing.
        assert decide_and_apply(root, read=no_prompt).capture == privacy.Choice(
            True, privacy.KEPT)

    @pytest.mark.parametrize("daylog", [{"sources": []}, {"capture": False},
                                        {"sources": [], "user_name": "Sam"}])
    def test_a_later_edit_to_off_is_kept(self, root: Path, daylog: dict[str, object]) -> None:
        record_a_yes(root, daylog)
        path = privacy.daylog_path(root)
        before = path.read_bytes()

        plan = decide_and_apply(root)

        assert plan.capture == privacy.Choice(False, privacy.KEPT)
        assert path.read_bytes() == before

    def test_a_recorded_no_stays_no(self, root: Path) -> None:
        record = {"capture": {"enabled": False, "how": "prompt", "at": "2026-11-01T09:00:00"}}
        privacy.record_path(root).parent.mkdir(parents=True)
        privacy.record_path(root).write_text(json.dumps(record), encoding="utf-8")

        plan = decide_and_apply(root)

        assert plan.capture == privacy.Choice(False, privacy.KEPT)
        assert not privacy.daylog_path(root).exists()


# ── The backup remote ───────────────────────────────────────────────


class TestRemote:
    def test_no_repository_skips_the_question(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        read, asked = answers("y")

        plan = decide_and_apply(root, interactive=True, read=read)

        assert len(asked) == 1  # capture only
        assert plan.remote == privacy.Choice(None, privacy.SKIPPED)
        # Wrapped prose: where a line breaks depends on the path length, so compare
        # with the wrapping undone (CI on Linux split this phrase across lines).
        assert "isn't a git repository" in " ".join(capsys.readouterr().out.split())
        text = privacy.summary(root, plan)
        assert "committed: nothing" in text
        assert "pushed:    nothing" in text

    @needs_git
    def test_repository_without_origin_skips_the_question(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        git_repo(root)
        read, asked = answers("y")

        plan = decide_and_apply(root, interactive=True, read=read)

        assert len(asked) == 1
        assert plan.git == privacy.GitState(is_repo=True)
        # Wrapped prose: where a line breaks depends on the path length, so compare
        # with the wrapping undone (CI on Linux split this phrase across lines).
        assert "no remote named origin" in " ".join(capsys.readouterr().out.split())
        assert "pushed:    nothing; there is no remote named origin" in privacy.summary(root, plan)

    @needs_git
    def test_remote_is_asked_and_recorded(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        git_repo(root, REMOTE)
        read, asked = answers("y", "y")

        plan = decide_and_apply(root, interactive=True, read=read)

        assert asked[1] == f"{privacy.REMOTE_QUESTION.format(url=REMOTE)} [y/n] "
        assert plan.remote == privacy.Choice(True, privacy.PROMPT)
        out = capsys.readouterr().out
        assert "plaintext" in out and "git-crypt" in out
        record = json.loads(privacy.record_path(root).read_text(encoding="utf-8"))
        assert record["remote"]["urls"] == [REMOTE]
        assert record["remote"]["private"] is True

        again = decide_and_apply(root, interactive=True, read=no_prompt)
        assert again.remote == privacy.Choice(True, privacy.KEPT)

    @needs_git
    def test_a_new_remote_is_asked_again(self, root: Path) -> None:
        git_repo(root, REMOTE)
        decide_and_apply(root, capture=True, remote_private=True)
        subprocess.run(["git", "-C", str(root), "remote", "set-url", "origin",
                        "https://example.com/sam/other.git"], check=True)
        read, asked = answers("n")

        plan = decide_and_apply(root, interactive=True, read=read)

        assert len(asked) == 1
        assert plan.remote == privacy.Choice(False, privacy.PROMPT)

    @needs_git
    def test_every_push_url_is_a_destination(
        self, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`git push origin` pushes to each pushurl, not only the first."""
        git_repo(root, REMOTE)
        other = "https://sam:ghp_x@mirror.example.com/sam/ari.git"
        for push_url in (REMOTE, other):
            subprocess.run(["git", "-C", str(root), "remote", "set-url", "--add", "--push",
                            "origin", push_url], check=True)
        both = f"{REMOTE} and https://mirror.example.com/sam/ari.git"

        state = privacy.inspect_git(root)
        assert state.remote_urls == (REMOTE, "https://mirror.example.com/sam/ari.git")

        read, asked = answers("y", "n")
        plan = decide_and_apply(root, interactive=True, read=read)
        assert asked[1] == f"Are {both} all private repositories that only you can read? [y/n] "
        out = " ".join(capsys.readouterr().out.split())  # undo the wrapping
        assert f"every push URL of origin: {both}" in out

        text = privacy.summary(root, plan)
        assert f"to {both}, in plaintext" in text
        assert "NOT all private, you said" in text
        assert "ghp_x" not in text
        record = json.loads(privacy.record_path(root).read_text(encoding="utf-8"))
        assert record["remote"]["urls"] == sorted(state.remote_urls)

    @needs_git
    def test_adding_a_push_url_makes_the_answer_stale(self, root: Path) -> None:
        git_repo(root, REMOTE)
        decide_and_apply(root, capture=True, remote_private=True)
        assert decide_and_apply(root).remote == privacy.Choice(True, privacy.KEPT)

        for push_url in (REMOTE, "https://example.com/public/ari.git"):
            subprocess.run(["git", "-C", str(root), "remote", "set-url", "--add", "--push",
                            "origin", push_url], check=True)

        plan = decide_and_apply(root)
        assert plan.remote == privacy.Choice(None, privacy.DEFAULT)
        text = privacy.summary(root, plan)
        assert "nobody has said they are all private" in text
        assert "WARNING: your conversations would go in plaintext" in text

        read, asked = answers("y")
        assert decide_and_apply(root, interactive=True, read=read).remote.how == privacy.PROMPT
        assert len(asked) == 1

    @needs_git
    def test_no_terminal_and_no_flag_is_not_private(self, root: Path) -> None:
        git_repo(root, REMOTE)
        write_daylog(root, {"user_name": "Sam"})

        plan = decide_and_apply(root)

        assert plan.remote == privacy.Choice(None, privacy.DEFAULT)
        assert "remote" not in privacy.read_record(root)
        text = privacy.summary(root, plan)
        assert "nobody has said it is private" in text
        assert "git-crypt init" in text

    def test_remote_flag_without_a_remote_is_ignored_with_a_note(self, root: Path) -> None:
        plan = decide_and_apply(root, capture=False, remote_private=True)
        assert plan.remote == privacy.Choice(None, privacy.SKIPPED)
        assert "ignored: there is no remote" in privacy.summary(root, plan)

    @needs_git
    def test_ignore_rules_are_read_from_the_repository(self, root: Path) -> None:
        git_repo(root, REMOTE)
        assert privacy.inspect_git(root).log_ignored is False
        (root / ".gitignore").write_text("log/\n", encoding="utf-8")
        assert privacy.inspect_git(root).log_ignored is True

    @needs_git
    def test_covering_only_the_jsonl_is_not_enough(self, root: Path) -> None:
        """The day's render, DD.md, holds the same turns."""
        git_repo(root, REMOTE)
        (root / ".gitattributes").write_text(
            "log/**/*.jsonl filter=git-crypt diff=git-crypt\n", encoding="utf-8"
        )
        (root / ".gitignore").write_text("log/**/*.jsonl\n", encoding="utf-8")
        state = privacy.inspect_git(root)
        assert state.log_encrypted is False
        assert state.log_ignored is False

    @needs_git
    def test_ignoring_log_after_the_fact_leaves_tracked_days_committed(self, root: Path) -> None:
        """An ignore rule doesn't untrack a file, and `git add -A` keeps staging it."""
        git_repo(root, REMOTE)
        track(root, "log/2026/11/04.jsonl")
        track(root, "log/2026/11/04.md", "# 4 November\n")
        (root / ".gitignore").write_text("log/\n", encoding="utf-8")

        state = privacy.inspect_git(root)

        assert state.log_ignored is True
        assert state.log_tracked == 2
        assert state.log_committed is True

    @needs_git
    def test_tracked_runtime_state_is_not_a_conversation(self, root: Path) -> None:
        git_repo(root, REMOTE)
        track(root, "log/.nightly-status.json", "{}\n")
        (root / ".gitignore").write_text("log/\n", encoding="utf-8")

        state = privacy.inspect_git(root)

        assert state.log_tracked == 0
        assert state.log_committed is False

    @needs_git
    def test_every_tracked_day_must_be_marked_for_git_crypt(self, root: Path) -> None:
        """New days marked, and ignored anyway; an old tracked day not marked."""
        git_repo(root, REMOTE)
        track(root, "log/2025/01/02.jsonl")
        (root / ".gitignore").write_text("log/\n", encoding="utf-8")
        (root / ".gitattributes").write_text(
            "log/2000/** filter=git-crypt diff=git-crypt\n", encoding="utf-8"
        )
        assert privacy.inspect_git(root).log_encrypted is False

    def test_git_crypt_status_output_is_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lines as git-crypt 0.7.0 prints them."""
        out = (
            "not encrypted: .gitattributes\n"
            "    encrypted: log/2026/11/04.jsonl\n"
            "    encrypted: log/2026/11/05.jsonl *** WARNING: staged/committed version is "
            "NOT ENCRYPTED! ***\n"
            "not encrypted: notes/a.md\n"
            "\n"
            "Warning: one or more files is marked for encryption via .gitattributes but\n"
        )

        def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, stdout=out, stderr="")

        monkeypatch.setattr(privacy.subprocess, "run", run)
        assert privacy.git_crypt_status(Path("."), "git-crypt") == {
            ".gitattributes": False,
            "log/2026/11/04.jsonl": True,
            "log/2026/11/05.jsonl": False,
            "notes/a.md": False,
        }

    def test_git_that_cannot_run_is_reported(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (root / ".git").mkdir(parents=True)

        def broken(*_args: object, **_kwargs: object) -> object:
            raise FileNotFoundError("git")

        monkeypatch.setattr(privacy.subprocess, "run", broken)
        state = privacy.inspect_git(root)
        assert state.is_repo and state.remote_urls == () and state.error

    def test_credentials_are_removed_from_the_url(self) -> None:
        assert privacy.redact_url("https://sam:ghp_x@example.com/a.git") == (
            "https://example.com/a.git"
        )
        assert privacy.redact_url("https://ghp_x@example.com/a.git") == (
            "https://example.com/a.git"
        )
        assert privacy.redact_url(REMOTE) == REMOTE


# ── The summary ─────────────────────────────────────────────────────


class TestSummary:
    @needs_git
    def test_names_what_is_pushed_where_and_how(self, root: Path) -> None:
        git_repo(root, REMOTE)

        plan = decide_and_apply(root, capture=True, remote_private=False)
        text = privacy.summary(root, plan)

        sources = "Claude Code transcripts, Codex rollouts and voice transcripts"
        assert f"captured:  {sources}, copied into {root / 'log'}" in text
        assert "committed: the memory tree, with the captured conversations" in text
        assert (f"pushed:    the memory tree, with the captured conversations, to {REMOTE}, "
                "in plaintext") in text
        assert "NOT private, you said" in text
        assert "WARNING: your conversations would go in plaintext" in text
        assert "git-crypt init" in text

    @needs_git
    def test_a_private_remote_gets_no_warning(self, root: Path) -> None:
        git_repo(root, REMOTE)
        plan = decide_and_apply(root, capture=True, remote_private=True)
        text = privacy.summary(root, plan)
        assert "private, you said" in text
        assert "WARNING" not in text
        assert "git-crypt init" not in text

    @needs_git
    def test_capture_off_still_says_the_tree_is_pushed(self, root: Path) -> None:
        git_repo(root, REMOTE)
        plan = decide_and_apply(root, capture=False, remote_private=False)
        text = privacy.summary(root, plan)
        assert "captured:  nothing" in text
        assert f"pushed:    the memory tree, to {REMOTE}, in plaintext" in text
        assert "WARNING" not in text  # no conversations go

    @needs_git
    def test_tracked_days_are_still_pushed_after_log_is_ignored(self, root: Path) -> None:
        git_repo(root, REMOTE)
        track(root, "log/2026/11/04.jsonl")
        (root / ".gitignore").write_text("log/\n", encoding="utf-8")

        plan = decide_and_apply(root, capture=True, remote_private=False)
        text = privacy.summary(root, plan)

        assert "pushed:    the memory tree, with the captured conversations, to" in text
        assert "1 day-log file(s) committed earlier are still tracked" in text
        assert "so not the conversations" not in text
        assert "WARNING: your conversations would go in plaintext" in text

    @needs_git
    def test_the_untrack_advice_names_each_file_and_works(self, root: Path) -> None:
        """No directory, no pattern: exactly the tracked day-log files, --cached,
        a dry run first. And the printed command really does untrack them."""
        import shlex
        import subprocess

        git_repo(root, REMOTE)
        days = ["log/2026/11/03.jsonl", "log/2026/11/04.jsonl", "log/2026.md"]
        for rel in days:
            track(root, rel)
        track(root, "notes/keep.md")
        (root / ".gitignore").write_text("log/\n", encoding="utf-8")

        plan = decide_and_apply(root, capture=True, remote_private=False)
        text = privacy.summary(root, plan)
        dry, real = privacy.untrack_commands(root, privacy.inspect_git(root))
        assert dry in text and real in text and "--dry-run" in dry
        for cmd in (dry, real):
            assert "--cached" in cmd and " -r" not in cmd and "*" not in cmd
            assert cmd.endswith(" -- " + " ".join(sorted(days)))
            assert "log/ " not in cmd + " "  # never the directory itself

        # Run the printed command as a user would (Windows-style quoting kept literal).
        argv = [token.strip('"') for token in shlex.split(real, posix=False)]
        subprocess.run(argv, check=True, capture_output=True)
        tracked = subprocess.run(["git", "-C", str(root), "ls-files"], check=True,
                                 capture_output=True, text=True).stdout.split()
        assert "notes/keep.md" in tracked and not set(days) & set(tracked)
        assert all((root / rel).exists() for rel in days)  # still on disk

    @needs_git
    def test_many_tracked_days_go_in_a_list_file_outside_the_tree(self, root: Path) -> None:
        git_repo(root, REMOTE)
        days = [f"log/2026/10/{d:02d}.jsonl" for d in range(1, 16)]
        for rel in days:
            track(root, rel)
        (root / ".gitignore").write_text("log/\n", encoding="utf-8")

        dry, real = privacy.untrack_commands(root, privacy.inspect_git(root))
        listing = Path(dry.split('--pathspec-from-file="', 1)[1].rstrip('"'))
        assert root.resolve() not in listing.resolve().parents  # never committed itself
        assert listing.read_text(encoding="utf-8").split() == sorted(days)
        assert "*" not in real and " -r" not in real and "--cached" in real

    @needs_git
    def test_waiting_files_are_listed_and_never_deleted(self, root: Path) -> None:
        git_repo(root, REMOTE)
        day = root / "log" / "2026" / "11" / "04.jsonl"
        day.parent.mkdir(parents=True)
        day.write_text("{}\n", encoding="utf-8")

        plan = decide_and_apply(root, capture=False, remote_private=False)
        text = privacy.summary(root, plan)
        assert "log/2026/11/04.jsonl" in text and "without deleting anything" in text
        assert "delete them" not in text and day.exists()

    @needs_git
    def test_tracked_days_are_pushed_even_with_capture_off(self, root: Path) -> None:
        git_repo(root, REMOTE)
        track(root, "log/2026/11/04.jsonl")

        plan = decide_and_apply(root, capture=False, remote_private=False)
        text = privacy.summary(root, plan)

        assert "captured:  nothing" in text
        assert "pushed:    the memory tree, with the captured conversations, to" in text
        assert "WARNING" in text

    @needs_git
    def test_days_on_disk_before_the_first_backup_go_with_it(self, root: Path) -> None:
        """Captured before any backup, then --no-capture: `git add -A` still takes them."""
        git_repo(root, REMOTE)
        day = root / "log" / "2026" / "11" / "04.jsonl"
        day.parent.mkdir(parents=True)
        day.write_text('{"who": "user", "text": "hi"}\n', encoding="utf-8")
        (root / "log" / ".nightly-status.json").write_text("{}\n", encoding="utf-8")

        state = privacy.inspect_git(root)
        assert (state.log_tracked, state.log_untracked) == (0, 1)
        assert state.log_committed is True

        plan = decide_and_apply(root, capture=False, remote_private=False)
        text = privacy.summary(root, plan)

        assert "captured:  nothing" in text
        assert "pushed:    the memory tree, with the captured conversations, to" in text
        assert "1 day-log file(s) it wrote earlier are on disk and not yet committed" in text
        assert "WARNING: your conversations would go in plaintext" in text

    @needs_git
    def test_ignored_days_on_disk_are_not_counted(self, root: Path) -> None:
        git_repo(root, REMOTE)
        day = root / "log" / "2026" / "11" / "04.jsonl"
        day.parent.mkdir(parents=True)
        day.write_text("{}\n", encoding="utf-8")
        (root / ".gitignore").write_text("log/\n", encoding="utf-8")

        state = privacy.inspect_git(root)
        assert state.log_untracked == 0
        assert state.log_committed is False

    @needs_git
    def test_a_git_ignored_day_log_is_not_called_pushed(self, root: Path) -> None:
        git_repo(root, REMOTE)
        (root / ".gitignore").write_text("log/\n", encoding="utf-8")
        plan = decide_and_apply(root, capture=True, remote_private=False)
        text = privacy.summary(root, plan)
        assert "log/ is git-ignored, so not the conversations" in text
        assert "WARNING" not in text


# ── git-crypt: verified, not assumed ────────────────────────────────


@needs_git
class TestGitCrypt:
    """A git-crypt attribute is only a request; git passes a filter it has no
    configuration for straight through, and commits plaintext."""

    def _summary(self, root: Path) -> str:
        plan = decide_and_apply(root, capture=True, remote_private=False)
        return privacy.summary(root, plan)

    def test_an_attribute_without_the_filter_configured_is_plaintext(self, root: Path) -> None:
        """A copied .gitattributes, or a clone never unlocked."""
        git_repo(root, REMOTE)
        mark_log_for_git_crypt(root)

        state = privacy.inspect_git(root)
        assert state.log_crypt == privacy.CRYPT_INACTIVE
        assert state.log_encrypted is False

        text = self._summary(root)
        assert f"to {REMOTE}, in plaintext" in text
        assert "this clone has no git-crypt filter configured" in text
        assert "WARNING: your conversations would go in plaintext" in text

    def test_configured_but_no_git_crypt_to_check_is_unverified(self, root: Path) -> None:
        git_repo(root, REMOTE)
        mark_log_for_git_crypt(root)
        configure_git_crypt_filter(root)

        assert privacy.inspect_git(root).log_crypt == privacy.CRYPT_UNVERIFIED
        text = self._summary(root)
        assert "in plaintext unless git-crypt encrypts log/ (unverified)" in text
        assert "isn't on PATH" in text
        assert "WARNING: your conversations would go in plaintext" in text

    def test_configured_and_confirmed_is_encrypted(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git_repo(root, REMOTE)
        track(root, "log/2026/11/04.jsonl")
        mark_log_for_git_crypt(root)
        configure_git_crypt_filter(root)
        fake_git_crypt(monkeypatch, {"log/2026/11/04.jsonl": True, ".gitattributes": False})

        assert privacy.inspect_git(root).log_crypt == privacy.CRYPT_ENCRYPTED
        text = self._summary(root)
        assert "log/ encrypted by git-crypt, everything else in plaintext" in text
        assert "WARNING" not in text

    def test_a_day_staged_before_git_crypt_is_plaintext(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git_repo(root, REMOTE)
        track(root, "log/2026/11/04.jsonl")
        track(root, "log/2026/11/05.jsonl")
        mark_log_for_git_crypt(root)
        configure_git_crypt_filter(root)
        fake_git_crypt(monkeypatch, {"log/2026/11/04.jsonl": True,
                                     "log/2026/11/05.jsonl": False})

        state = privacy.inspect_git(root)
        assert state.log_crypt == privacy.CRYPT_INACTIVE
        assert "1 tracked day-log file(s) are staged unencrypted" in state.crypt_detail
        assert "WARNING" in self._summary(root)

    def test_a_tracked_day_git_crypt_does_not_list_is_not_assumed_encrypted(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git_repo(root, REMOTE)
        track(root, "log/2026/11/04.jsonl")
        mark_log_for_git_crypt(root)
        configure_git_crypt_filter(root)
        fake_git_crypt(monkeypatch, {})

        assert privacy.inspect_git(root).log_crypt == privacy.CRYPT_INACTIVE

    def test_the_days_alone_leave_digests_and_rollups_in_plaintext(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The nightly also writes DD-digest.md and week, month and year rollups."""
        git_repo(root, REMOTE)
        days_only = "log/**/*.jsonl {0}\nlog/*/*/[0-9][0-9].md {0}\n"
        (root / ".gitattributes").write_text(
            days_only.format("filter=git-crypt diff=git-crypt"), encoding="utf-8"
        )
        (root / ".gitignore").write_text(days_only.format("").replace(" \n", "\n"),
                                         encoding="utf-8")
        configure_git_crypt_filter(root)
        fake_git_crypt(monkeypatch, {})

        state = privacy.inspect_git(root)

        assert state.log_ignored is False
        assert state.log_crypt == privacy.CRYPT_NONE
        assert "WARNING: your conversations would go in plaintext" in self._summary(root)

    @pytest.mark.parametrize("left_out", privacy._SAMPLE_LOGS)
    def test_every_output_must_be_covered(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, left_out: str
    ) -> None:
        git_repo(root, REMOTE)
        others = [p for p in privacy._SAMPLE_LOGS if p != left_out]
        (root / ".gitattributes").write_text(
            "".join(f"{p} filter=git-crypt diff=git-crypt\n" for p in others), encoding="utf-8"
        )
        configure_git_crypt_filter(root)
        fake_git_crypt(monkeypatch, {})
        assert privacy.inspect_git(root).log_crypt == privacy.CRYPT_NONE

        (root / ".gitignore").write_text("".join(f"{p}\n" for p in others), encoding="utf-8")
        assert privacy.inspect_git(root).log_ignored is False

        (root / ".gitignore").write_text("log/\n", encoding="utf-8")
        assert privacy.inspect_git(root).log_ignored is True

    def test_git_crypt_status_failing_is_unverified(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git_repo(root, REMOTE)
        mark_log_for_git_crypt(root)
        configure_git_crypt_filter(root)
        fake_git_crypt(monkeypatch, None)

        assert privacy.inspect_git(root).log_crypt == privacy.CRYPT_UNVERIFIED
        assert "WARNING" in self._summary(root)

    @pytest.mark.skipif(shutil.which("git-crypt") is None, reason="git-crypt is not installed")
    def test_against_the_real_git_crypt(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A day committed before `git-crypt init` stays plaintext until re-staged."""
        monkeypatch.setattr(privacy, "git_crypt_exe", lambda: shutil.which("git-crypt"))
        git_repo(root, REMOTE)
        track(root, "log/2026/11/04.jsonl")
        subprocess.run(["git-crypt", "init"], cwd=root, check=True, capture_output=True)
        mark_log_for_git_crypt(root)

        assert privacy.inspect_git(root).log_crypt == privacy.CRYPT_INACTIVE

        subprocess.run(["git-crypt", "status", "-f"], cwd=root, capture_output=True)
        assert privacy.inspect_git(root).log_crypt == privacy.CRYPT_ENCRYPTED


# ── scripts/install.py ──────────────────────────────────────────────


@pytest.fixture()
def installer() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
    spec = importlib.util.spec_from_file_location("smriti_install_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def no_tty(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """stdin is not a terminal, and input() must never be called."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr("builtins.input", no_prompt)
    yield


def run_installer(installer: ModuleType, root: Path, *flags: str) -> int:
    argv = [
        "--harness", "none",
        "--memory-root", str(root),
        "--projects-root", str(root.parent / "no-projects"),
        *flags,
    ]
    result = installer.main(argv)
    assert isinstance(result, int)
    return result


@pytest.mark.usefixtures("no_tty")
class TestInstaller:
    def test_no_capture_flag_without_a_terminal(
        self, installer: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run_installer(installer, root, "--no-capture") == 0

        assert read_daylog(root) == {"capture": False, "sources": []}
        assert (root / ".smriti" / "wake.py").exists()  # the core install ran
        out = capsys.readouterr().out
        assert "captured:  nothing" in out
        assert "a running smriti logd rereads .smriti/daylog.json when it changes" in out
        assert out.rstrip().endswith("pushed:    nothing")  # the summary is last

    def test_capture_flag_without_a_terminal(
        self, installer: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run_installer(installer, root, "--capture") == 0

        assert read_daylog(root) == {"capture": True}
        record = json.loads(privacy.record_path(root).read_text(encoding="utf-8"))
        assert record["capture"]["enabled"] is True
        assert record["capture"]["how"] == "flag"
        out = capsys.readouterr().out
        assert "Claude Code transcripts, Codex rollouts and voice transcripts" in out

    def test_no_flag_and_no_terminal_captures_nothing(
        self, installer: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run_installer(installer, root) == 0

        assert read_daylog(root) == {"capture": False, "sources": []}
        assert "capture is OFF: there was no terminal" in capsys.readouterr().out

    def test_rerun_keeps_an_existing_daylog(
        self, installer: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_daylog(root, {"user_name": "Sam", "agent_name": "Ari",
                                   "notify_cmd": ["notify"], "capture": True})
        before = path.read_bytes()

        assert run_installer(installer, root) == 0

        assert path.read_bytes() == before
        assert "capture is on because" in capsys.readouterr().out

    def test_malformed_daylog_refuses_before_any_change(
        self, installer: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = privacy.daylog_path(root)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        assert run_installer(installer, root, "--no-capture") == 1

        assert "REFUSED before any change" in capsys.readouterr().err
        assert path.read_text(encoding="utf-8") == "{not json"
        assert not (root / ".smriti" / "wake.py").exists()
        assert not (root / "identity.md").exists()

    def test_contradictory_flags_are_rejected(self, installer: ModuleType, root: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            run_installer(installer, root, "--capture", "--no-capture")
        assert exc.value.code == 2
        with pytest.raises(SystemExit):
            run_installer(installer, root, "--remote-is-private", "--remote-not-private")

    @needs_git
    def test_summary_names_what_is_pushed(
        self, installer: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        git_repo(root, "https://sam:ghp_secret@example.com/sam/ari-memory.git")

        assert run_installer(installer, root, "--capture", "--remote-not-private") == 0

        out = capsys.readouterr().out
        assert "ghp_secret" not in out
        assert "captured:  Claude Code transcripts, Codex rollouts and voice transcripts" in out
        assert ("pushed:    the memory tree, with the captured conversations, to "
                "https://example.com/sam/ari-memory.git, in plaintext") in out
        assert "git-crypt init" in out


def test_capture_on_clears_capture_false_beside_custom_sources(tmp_path: Path) -> None:
    """Codex review of H11.1: with "capture": false next to a custom sources list,
    --capture returned early and capture stayed off, since false outranks the list."""
    config = tmp_path / ".smriti" / "daylog.json"
    config.parent.mkdir(parents=True)
    custom = [{"name": "notes", "kind": "voice_md", "glob": "voice/*.md"}]
    config.write_text(json.dumps({"capture": False, "sources": custom, "location": "x"}),
                      encoding="utf-8")
    assert privacy.set_capture(tmp_path, True)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert "capture" not in data and data["sources"] == custom and data["location"] == "x"
    assert privacy.capture_sources(tmp_path)  # captures its own sources now
