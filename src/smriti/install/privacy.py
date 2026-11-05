"""The installer's two privacy questions: capture, and the backup remote.

Left to its defaults, the day-log copies every conversation the agent has
(Claude Code transcripts, Codex rollouts, voice transcripts) into the
memory tree's ``log/``, and the backup commits the tree and pushes it to
the git remote ``origin``, in plaintext unless git-crypt encrypts it. Only
a pattern-based credential scrubber stands in between. So the installer
makes the person choose, and says what follows:

1. Capture conversations into the day-log? "No" writes ``"sources": []``
   into ``<root>/.smriti/daylog.json`` and keeps every other key.
2. Is the backup remote private? Asked only when there is a remote.
   smriti never sets up git-crypt; it prints the steps instead.

A flag answers a question; otherwise a terminal is asked. With neither, a
new memory root captures nothing, because capture can be turned on later
(the nightly then reads the transcripts still on disk) while a push can't
be taken back; an existing root keeps the sources its ``daylog.json`` has.
A person's answers are recorded in ``<root>/.smriti/privacy.json``, so a
rerun doesn't ask again.

The installer calls :func:`decide` before it changes anything, then
:func:`apply` after the core install, and prints :func:`summary` last.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smriti.daylog.config import SourceSpec, load_config

GIT_CRYPT_URL = "https://github.com/AGWA/git-crypt"
PRIVACY_DOC_URL = (
    "https://github.com/Stormbane/smriti/blob/master/docs/PRIVACY.md#what-is-pushed-in-the-clear"
)
_REPO_ROOT = Path(__file__).resolve().parents[3]

DAYLOG_NAME = "daylog.json"
RECORD_NAME = "privacy.json"

# One path for each file capture and the nightly write under log/ that
# holds a conversation or a summary of one (smriti.daylog.config's day_*
# helpers and smriti.daylog.rollup). They need not exist: git answers
# ignore and attribute questions from the patterns alone. The day-log is
# ignored, or encrypted, only when every one of them is.
_SAMPLE_LOGS = (
    "log/2000/01/01.jsonl",  # the day's turns
    "log/2000/01/01.md",  # their render
    "log/2000/01/01-digest.md",  # the nightly digest
    "log/2000/01/week1.md",  # the week rollup
    "log/2000/01.md",  # the month rollup
    "log/2000.md",  # the year rollup
)

# The day-log's dated files: each day's turns and render, its digest, and
# the week, month and year rollups (log/YYYY/..., log/YYYY.md). Runtime
# state such as log/.nightly-status.json holds no conversation.
_CONVERSATION = re.compile(r"^log/\d{4}(?:/|\.md$)")

_KIND_LABELS = {
    "claude_jsonl": "Claude Code transcripts",
    "codex_rollout": "Codex rollouts",
    "voice_md": "voice transcripts",
}

# How a choice was made. Only a person's answer (a flag or a prompt) is
# final; a default is asked again the next time there is a terminal.
FLAG = "flag"
PROMPT = "prompt"
KEPT = "kept"
DEFAULT = "default"
SKIPPED = "skipped"
_ANSWERED = (FLAG, PROMPT)

CAPTURE_QUESTION = "Capture conversations into the day-log?"
REMOTE_QUESTION = "Is {url} a private repository that only you can read?"

Ask = Callable[[str], str]


class PrivacyConfigError(Exception):
    """``daylog.json`` isn't a readable JSON object, so it can't be edited safely."""


@dataclass(frozen=True)
class GitState:
    """What the backup would do with the memory tree, as git sees it now."""

    is_repo: bool
    remote_url: str | None = None  # origin's push URL, credentials removed
    log_ignored: bool = False  # git ignores the day-log files capture will write
    # Day-log files git already tracks. An ignore rule doesn't untrack them,
    # and backup.py's `git add -A` keeps committing their changes.
    log_tracked: int = 0
    # Day-log files on disk, not ignored and not yet committed: written
    # before the first backup, they go with the next one, capture on or off.
    log_untracked: int = 0
    log_crypt: str = "none"  # one of CRYPT_STATES: how well git-crypt protects them
    crypt_detail: str = ""  # why, when it isn't plainly encrypted or plainly not
    error: str = ""  # git couldn't be run

    @property
    def log_on_disk(self) -> int:
        """Day-log files the next backup commits whatever capture does from now on."""
        return self.log_tracked + self.log_untracked

    @property
    def log_committed(self) -> bool:
        """Conversations reach a commit: new ones aren't ignored, or existing ones are."""
        return not self.log_ignored or self.log_on_disk > 0

    @property
    def log_encrypted(self) -> bool:
        """Only a verified encryption counts; anything less is reported as plaintext."""
        return self.log_crypt == CRYPT_ENCRYPTED


# How well git-crypt protects the day-log the backup commits, weakest first.
# A .gitattributes line alone proves nothing: git treats a filter it has no
# configuration for as a passthrough, so a copied .gitattributes, or a clone
# that was never unlocked, commits plaintext under a git-crypt attribute.
CRYPT_NONE = "none"  # not every committed day-log file is marked for git-crypt
CRYPT_INACTIVE = "inactive"  # marked, but this clone commits plaintext anyway
CRYPT_UNVERIFIED = "unverified"  # marked and configured; git-crypt isn't here to confirm
CRYPT_ENCRYPTED = "encrypted"  # configured, and git-crypt reports each tracked day encrypted
CRYPT_STATES = (CRYPT_NONE, CRYPT_INACTIVE, CRYPT_UNVERIFIED, CRYPT_ENCRYPTED)


@dataclass(frozen=True)
class Choice:
    value: bool | None  # None: no remote, or nobody said
    how: str  # FLAG, PROMPT, KEPT, DEFAULT or SKIPPED


@dataclass(frozen=True)
class PrivacyPlan:
    capture: Choice
    remote: Choice
    git: GitState
    notices: tuple[str, ...] = ()


# ── Files ───────────────────────────────────────────────────────────


def daylog_path(root: Path) -> Path:
    return root / ".smriti" / DAYLOG_NAME


def record_path(root: Path) -> Path:
    return root / ".smriti" / RECORD_NAME


def read_daylog(root: Path) -> dict[str, Any] | None:
    """The raw ``daylog.json``, or None when there is none.

    Raises :class:`PrivacyConfigError` for a file that isn't a JSON object:
    capture would silently fall back to the defaults, and rewriting it would
    lose whatever it holds.
    """
    path = daylog_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PrivacyConfigError(f"can't read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PrivacyConfigError(
            f"{path} isn't valid JSON ({exc}), so capture ignores it and runs on the "
            "defaults. Fix it, then re-run the installer."
        ) from exc
    if not isinstance(data, dict):
        raise PrivacyConfigError(f"{path} isn't a JSON object. Fix it, then re-run the installer.")
    return data


def read_record(root: Path) -> dict[str, Any]:
    """The recorded answers; empty when there are none or the file is unreadable."""
    try:
        data = json.loads(record_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _answer(record: dict[str, Any], key: str) -> dict[str, Any] | None:
    entry = record.get(key)
    if isinstance(entry, dict) and entry.get("how") in _ANSWERED:
        return entry
    return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(tmp, path)


def set_capture(root: Path, enabled: bool) -> str | None:
    """Make ``daylog.json`` capture, or capture nothing. Returns what changed.

    Only the ``sources`` key is touched. Off sets it to ``[]``, creating the
    file if needed; on removes an empty ``sources`` so the defaults apply,
    and leaves a list of the file's own sources alone. ``extra_sources``
    is never touched, so its sources are still captured either way.
    """
    path = daylog_path(root)
    data = read_daylog(root)
    if enabled:
        if data is None or data.get("sources") != []:
            return None
        del data["sources"]
        _write_json(path, data)
        return f'removed "sources": [] from {path}, so the default sources apply'
    if data is not None and data.get("sources") == []:
        return None
    data = {} if data is None else data
    data["sources"] = []
    _write_json(path, data)
    return f'set "sources": [] in {path}'


# ── State ───────────────────────────────────────────────────────────


def capture_sources(root: Path) -> list[SourceSpec]:
    """The sources capture would read, exactly as the day-log resolves them."""
    return load_config(root).sources


def describe_sources(sources: list[SourceSpec]) -> str:
    labels: list[str] = []
    for spec in sources:
        label = _KIND_LABELS.get(spec.kind, f"{spec.name} ({spec.kind})")
        if label not in labels:
            labels.append(label)
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def is_existing_install(root: Path) -> bool:
    """True when smriti was installed here before, or has captured here."""
    return (
        daylog_path(root).exists()
        or (root / ".smriti" / "wake.py").exists()
        or (root / "log").is_dir()
    )


_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@")


def redact_url(url: str) -> str:
    """The URL without its ``user:password@`` part, which can hold a token."""
    return _USERINFO.sub(r"\1", url)


def _git(root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL, timeout=60,
    )


def _filter_attrs(root: Path, paths: list[str]) -> dict[str, str]:
    """Each path's ``filter`` attribute, read through stdin (no length limit)."""
    done = _git(root, "check-attr", "--stdin", "-z", "filter", stdin="\0".join(paths) + "\0")
    if done.returncode != 0:
        raise subprocess.SubprocessError(done.stderr.strip() or "git check-attr failed")
    fields = done.stdout.split("\0")
    return {fields[i]: fields[i + 2] for i in range(0, len(fields) - 2, 3)}


def git_crypt_exe() -> str | None:
    """The git-crypt binary on PATH, if there is one."""
    return shutil.which("git-crypt")


# `git-crypt status` lines: "    encrypted: <path>", "not encrypted: <path>",
# and for a file marked for encryption whose staged blob is plaintext,
# "    encrypted: <path> *** WARNING: staged/committed version is NOT ENCRYPTED! ***".
_CRYPT_STATUS = re.compile(r"^\s*(not encrypted|encrypted): (.*?)( \*\*\* WARNING.*)?$")


def git_crypt_status(root: Path, exe: str) -> dict[str, bool] | None:
    """Each file's path, and whether git-crypt says its staged version is
    encrypted. None when git-crypt can't say."""
    try:
        done = subprocess.run(
            [exe, "status"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode not in (0, 1):  # 1: some file is staged unencrypted
        return None
    status: dict[str, bool] = {}
    for line in done.stdout.splitlines():
        m = _CRYPT_STATUS.match(line)
        if m:
            status[m.group(2)] = m.group(1) == "encrypted" and m.group(3) is None
    return status


def _log_protection(
    root: Path, committed: list[str], tracked: list[str], attrs: dict[str, str]
) -> tuple[str, str]:
    """(one of CRYPT_STATES, why) for the day-log files the backup commits."""
    if not committed or not all(attrs.get(p) == "git-crypt" for p in committed):
        return CRYPT_NONE, ""
    clean = _git(root, "config", "--get", "filter.git-crypt.clean").stdout.strip()
    smudge = _git(root, "config", "--get", "filter.git-crypt.smudge").stdout.strip()
    if not (clean and smudge):
        return CRYPT_INACTIVE, (
            ".gitattributes marks log/ for git-crypt, but this clone has no git-crypt "
            "filter configured (git-crypt init, or git-crypt unlock after a clone), so git "
            "commits it in plaintext"
        )
    exe = git_crypt_exe()
    if exe is None:
        return CRYPT_UNVERIFIED, (
            "log/ is marked for git-crypt and its filter is configured, but git-crypt isn't "
            "on PATH, so smriti couldn't confirm it encrypts"
        )
    status = git_crypt_status(root, exe)
    if status is None:
        return CRYPT_UNVERIFIED, "git-crypt status failed, so smriti couldn't confirm it encrypts"
    plain = [p for p in tracked if status.get(p) is not True]
    if plain:
        return CRYPT_INACTIVE, (
            f"{len(plain)} tracked day-log file(s) are staged unencrypted (git-crypt status -f "
            "re-stages them; earlier commits stay readable)"
        )
    return CRYPT_ENCRYPTED, ""


def inspect_git(root: Path) -> GitState:
    """The repository, its ``origin`` (where ``backup.py`` pushes), and how
    git treats the day-log: which of it is committed, and whether git-crypt
    is asked to encrypt it."""
    if not (root / ".git").exists():  # backup.py's own test for a repository
        return GitState(is_repo=False)
    try:
        url = _git(root, "remote", "get-url", "--push", "origin")
        ignored = _git(root, "check-ignore", "--stdin", "-z",  # prints each ignored path
                       stdin="\0".join(_SAMPLE_LOGS) + "\0")
        listed = _git(root, "ls-files", "-z", "--", "log/")
        others = _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", "log/")
        for done in (listed, others):
            if done.returncode != 0:
                raise subprocess.SubprocessError(done.stderr.strip() or "git ls-files failed")
        tracked = [p for p in listed.stdout.split("\0") if _CONVERSATION.match(p)]
        untracked = [p for p in others.stdout.split("\0") if _CONVERSATION.match(p)]
        ignored_samples = set(ignored.stdout.split("\0")) & set(_SAMPLE_LOGS)
        log_ignored = ignored_samples == set(_SAMPLE_LOGS)
        # What the backup commits from log/: the tracked files, the untracked
        # ones that aren't ignored (`git add -A`), and each kind of new file
        # that isn't ignored.
        committed = (
            tracked + untracked + [p for p in _SAMPLE_LOGS if p not in ignored_samples]
        )
        attrs = _filter_attrs(root, committed) if committed else {}
        crypt, detail = _log_protection(root, committed, tracked, attrs)
    except (OSError, subprocess.SubprocessError) as exc:
        return GitState(is_repo=True, error=str(exc))
    remote: str | None = None
    if url.returncode == 0 and url.stdout.strip():
        remote = redact_url(url.stdout.strip())
    elif "no such remote" not in url.stderr.lower():
        return GitState(is_repo=True, error=url.stderr.strip() or f"git exited {url.returncode}")
    return GitState(
        is_repo=True,
        remote_url=remote,
        log_ignored=log_ignored,
        log_tracked=len(tracked),
        log_untracked=len(untracked),
        log_crypt=crypt,
        crypt_detail=detail,
    )


def is_interactive() -> bool:
    """A person can answer: both stdin and stdout are a terminal."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):  # no stdin (pythonw), or a closed one
        return False


# ── Questions ───────────────────────────────────────────────────────


def ask_yes_no(question: str, default: bool | None, read: Ask | None = None) -> bool | None:
    """y or n; Enter takes ``default`` when there is one. None on end of input.

    ``read`` defaults to :func:`input`, looked up at call time.
    """
    read = read or input
    hint = "[Y/n]" if default is True else "[y/N]" if default is False else "[y/n]"
    for _ in range(5):
        try:
            answer = read(f"{question} {hint} ").strip().lower()
        except EOFError:
            return None
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if not answer and default is not None:
            return default
        print("  Please answer y or n.")
    return None


def _para(text: str) -> str:
    # Paths and URLs stay whole, so they can be copied.
    return textwrap.fill(text, width=78, break_long_words=False, break_on_hyphens=False)


def privacy_doc() -> str:
    local = _REPO_ROOT / "docs" / "PRIVACY.md"
    if local.is_file():
        return f'{local}, "What is pushed in the clear"'
    return PRIVACY_DOC_URL


def capture_explanation(root: Path) -> str:
    return "\n" + _para(
        "Capture: the day-log copies every message you type and every reply the agent "
        "writes, from Claude Code transcripts, Codex rollouts and voice transcripts, into "
        f"{root / 'log'} (strings shaped like credentials are masked; nothing else is "
        "removed). The backup commits them with the rest of the memory tree, and pushes "
        "them to the tree's git remote if it has one."
    )


def remote_explanation(root: Path, git: GitState) -> str:
    if git.error:
        return "\n" + _para(
            f"Backup remote: git couldn't be run in {root} ({git.error}), so the backup "
            "can't commit or push either. Skipping this question."
        )
    if not git.is_repo:
        return "\n" + _para(
            f"Backup remote: {root} isn't a git repository, so the backup neither commits "
            "nor pushes. Skipping this question. If you add a remote later, the tree is "
            f"pushed in plaintext unless you set up git-crypt (see {privacy_doc()})."
        )
    if git.remote_url is None:
        return "\n" + _para(
            f"Backup remote: {root} has no remote named origin, so the backup commits on "
            "this machine and pushes nothing. Skipping this question."
        )
    if git.log_encrypted:
        crypt = "here it encrypts log/, but file names and commit messages stay readable"
    elif git.crypt_detail:
        crypt = f"here {git.crypt_detail}"
    else:
        crypt = "here it isn't set up for log/"
    return "\n" + _para(
        f"Backup remote: the hourly backup and the nightly push the memory tree to "
        f"origin, {git.remote_url}. Git sends files in plaintext unless git-crypt "
        f"encrypts them, and smriti doesn't set git-crypt up ({crypt}). See {privacy_doc()}."
    )


def git_crypt_steps(root: Path) -> str:
    return "\n".join([
        "  To keep the tree unreadable there, before backups run, either:",
        "    - make the repository private, or remove the remote:",
        f'        git -C "{root}" remote remove origin',
        f"    - or set up git-crypt ({GIT_CRYPT_URL}); smriti doesn't do it for you:",
        f'        cd "{root}"',
        "        git-crypt init",
        "        git-crypt export-key <a file outside this tree>   (lose it and nothing decrypts)",
        "        add to .gitattributes:   log/** filter=git-crypt diff=git-crypt",
        "        git-crypt status -f      (re-stages tracked files that should be encrypted)",
        "      File names, sizes and commit messages stay readable, and whatever was",
        "      pushed before stays readable in the remote's history.",
        f"  Details: {privacy_doc()}",
    ])


def decide(
    root: Path,
    *,
    capture: bool | None,
    remote_private: bool | None,
    interactive: bool,
    read: Ask | None = None,
) -> PrivacyPlan:
    """Settle both questions without changing anything on disk.

    ``capture`` and ``remote_private`` are the flags (None when absent).
    Raises :class:`PrivacyConfigError` before asking anything when
    ``daylog.json`` can't be edited safely.
    """
    read_daylog(root)
    record = read_record(root)
    existing = is_existing_install(root)
    current = bool(capture_sources(root))
    git = inspect_git(root)
    notices: list[str] = []

    choice: Choice | None = None
    if capture is not None:
        choice = Choice(capture, FLAG)
    elif _answer(record, "capture") is not None:
        choice = Choice(current, KEPT)  # chosen before; daylog.json holds it
    elif interactive:
        print(capture_explanation(root))
        answer = ask_yes_no(CAPTURE_QUESTION, current if existing else None, read)
        if answer is not None:
            choice = Choice(answer, PROMPT)
    if choice is None and existing:
        choice = Choice(current, KEPT)
        why = (
            ".smriti/daylog.json says so"
            if daylog_path(root).exists()
            else "there is no .smriti/daylog.json, so the default sources apply"
        )
        notices.append(
            f"capture is {'on' if current else 'off'} because {why}; nobody has chosen it "
            "at install. Pass --capture or --no-capture to choose."
        )
    elif choice is None:
        choice = Choice(False, DEFAULT)
        notices.append(
            "capture is OFF: there was no terminal to ask and no --capture or --no-capture "
            "flag. Re-run with --capture to turn it on; the nightly then reads the "
            "transcripts still on disk."
        )

    remote: Choice
    recorded = _answer(record, "remote")
    if git.remote_url is None:
        remote = Choice(None, SKIPPED)
        if interactive:
            print(remote_explanation(root, git))
        if remote_private is not None:
            notices.append("--remote-is-private/--remote-not-private ignored: there is no remote.")
    elif remote_private is not None:
        remote = Choice(remote_private, FLAG)
    elif (
        recorded is not None
        and recorded.get("url") == git.remote_url
        and isinstance(recorded.get("private"), bool)
    ):
        remote = Choice(recorded["private"], KEPT)
    elif interactive:
        print(remote_explanation(root, git))
        answer = ask_yes_no(REMOTE_QUESTION.format(url=git.remote_url), None, read)
        remote = Choice(answer, PROMPT if answer is not None else DEFAULT)
    else:
        remote = Choice(None, DEFAULT)
    if remote.how == DEFAULT:
        notices.append(
            "nobody has said whether the remote is private: pass --remote-is-private or "
            "--remote-not-private."
        )
    return PrivacyPlan(capture=choice, remote=remote, git=git, notices=tuple(notices))


def apply(root: Path, plan: PrivacyPlan, *, now: datetime | None = None) -> list[str]:
    """Write the capture choice into ``daylog.json`` and record the answers.

    A choice that was kept changes nothing. Returns what changed.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    changes: list[str] = []
    record = read_record(root)
    dirty = False
    if plan.capture.how in (FLAG, PROMPT, DEFAULT):
        enabled = bool(plan.capture.value)
        change = set_capture(root, enabled)
        if change:
            changes.append(change)
        record["capture"] = {"enabled": enabled, "how": plan.capture.how, "at": stamp}
        dirty = True
    if plan.remote.how in _ANSWERED:
        record["remote"] = {
            "url": plan.git.remote_url,
            "private": plan.remote.value,
            "how": plan.remote.how,
            "at": stamp,
        }
        dirty = True
    if dirty:
        _write_json(record_path(root), record)
        changes.append(f"recorded the answers in {record_path(root)}")
    return changes


# ── Summary ─────────────────────────────────────────────────────────


def summary(root: Path, plan: PrivacyPlan) -> str:
    """What is captured, what is committed, and where it is pushed, as of now.

    Git is asked again: the core install may have added the template's
    ``.gitignore`` since :func:`decide` looked.
    """
    sources = capture_sources(root)
    git = inspect_git(root)
    what = describe_sources(sources)
    lines = ["", "privacy:"]
    if sources:
        lines.append(f"  captured:  {what}, copied into {root / 'log'} as you talk")
    else:
        lines.append('  captured:  nothing ("sources": [] in .smriti/daylog.json)')

    # Day-log files already tracked, or on disk and not ignored, are
    # committed whether or not capture is on now.
    conversations = (bool(sources) and not git.log_ignored) or git.log_on_disk > 0
    pushes_plain = False
    if git.error or not git.is_repo:
        why = f"git couldn't be run ({git.error})" if git.error else f"{root} isn't a git repository"
        lines.append(f"  committed: nothing; {why}")
        lines.append("  pushed:    nothing")
    else:
        tree = "the memory tree" + (", with the captured conversations" if conversations else "")
        if git.log_ignored and not git.log_committed and sources:
            tree += " (log/ is git-ignored, so not the conversations)"
        lines.append(f"  committed: {tree}, by the hourly backup and the nightly")
        if git.log_ignored and git.log_tracked:
            lines.append(
                f"             log/ is git-ignored, but {git.log_tracked} day-log file(s) "
                "committed earlier are still tracked, and each backup commits their changes "
                "(`git rm -r --cached log/` untracks them; history keeps them)"
            )
        if not sources and git.log_untracked:
            lines.append(
                f"             capture is off, but {git.log_untracked} day-log file(s) it wrote "
                "earlier are on disk and not yet committed; the next backup commits them "
                "(delete them, or git-ignore log/, to keep them out)"
            )
        if git.remote_url is None:
            lines.append("  pushed:    nothing; there is no remote named origin")
        else:
            if git.log_encrypted:
                how = "log/ encrypted by git-crypt, everything else in plaintext"
            elif git.log_crypt == CRYPT_UNVERIFIED:
                how = "in plaintext unless git-crypt encrypts log/ (unverified)"
            else:
                how = "in plaintext"
            if plan.remote.value is True:
                said = "private, you said"
            elif plan.remote.value is False:
                said = "NOT private, you said"
            else:
                said = "nobody has said it is private"
            lines.append(f"  pushed:    {tree}, to {git.remote_url}, {how}")
            if git.crypt_detail and conversations:
                lines.append(f"  git-crypt: {git.crypt_detail}")
            lines.append(f"  remote:    {said}")
            pushes_plain = plan.remote.value is not True
        lines.append("  (the backup and the nightly run once `smriti tasks install` schedules them)")
    if pushes_plain and conversations and not git.log_encrypted:
        lines.append(
            "  WARNING: your conversations would go in plaintext to a remote nobody has "
            "said is private."
        )
    if pushes_plain:
        lines.append(git_crypt_steps(root))
    lines.extend(f"  note: {notice}" for notice in plan.notices)
    return "\n".join(lines)
