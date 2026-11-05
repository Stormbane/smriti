"""The installer's two privacy questions: capture, and the backup remote.

Once capture is on, the day-log copies every conversation the agent has
(Claude Code transcripts, Codex rollouts, voice transcripts) into the
memory tree's ``log/``, and the backup commits the tree and pushes it to
the current branch's upstream remote (``origin`` when it has none), in
plaintext unless git-crypt encrypts it. Only a pattern-based credential
scrubber stands in between. Capture is off until ``daylog.json`` turns it
on (:mod:`smriti.daylog.config`), so the installer makes the person
choose, and says what follows:

1. Capture conversations into the day-log? "Yes" writes ``"capture": true``
   (the default sources) into ``<root>/.smriti/daylog.json``; "No" writes
   ``"sources": []``. Either keeps every other key.
2. Is the backup remote private? Asked only when there is a remote.
   smriti never sets up git-crypt; it prints the steps instead.

A flag answers a question; otherwise a terminal is asked. With neither, a
new memory root captures nothing, because capture can be turned on later
(the nightly then reads the transcripts still on disk) while a push can't
be taken back; an existing root keeps what its ``daylog.json`` says. A
person's answers are recorded in ``<root>/.smriti/privacy.json``, so a
rerun doesn't ask again. A "yes" recorded before capture needed saying so
in ``daylog.json`` (it used to be the default) is written out there.

The installer calls :func:`decide` before it changes anything, then
:func:`apply` after the core install, and prints :func:`summary` last.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
# An answer recorded before H11.1, written into daylog.json: until then a
# "yes" left daylog.json to the default sources, which now capture nothing.
RECORDED = "recorded"
_ANSWERED = (FLAG, PROMPT)

CAPTURE_QUESTION = "Capture conversations into the day-log?"
REMOTE_QUESTION = "Is {url} a private repository that only you can read?"
REMOTE_QUESTION_MANY = "Are {urls} all private repositories that only you can read?"

Ask = Callable[[str], str]


class PrivacyConfigError(Exception):
    """``daylog.json`` isn't a readable JSON object, so it can't be edited safely."""


@dataclass(frozen=True)
class GitState:
    """What the backup would do with the memory tree, as git sees it now."""

    is_repo: bool
    # Every push URL of the remote the backup pushes to (see backup_remote),
    # credentials removed. `git push <remote>` pushes to all of them, so each
    # one is a destination.
    remote_urls: tuple[str, ...] = ()
    remote: str = "origin"  # that remote: a name, or a URL a branch's upstream names directly
    no_push: str = ""  # why the backup pushes nothing at all (a detached HEAD), else ""
    log_ignored: bool = False  # git ignores the day-log files capture will write
    # Day-log files git already tracks. An ignore rule doesn't untrack them,
    # and backup.py's `git add -A` keeps committing their changes.
    log_tracked: int = 0
    # Day-log files on disk, not ignored and not yet committed: written
    # before the first backup, they go with the next one, capture on or off.
    log_untracked: int = 0
    # The files themselves, so any advice names exactly them, never a pattern.
    tracked_files: tuple[str, ...] = ()
    untracked_files: tuple[str, ...] = ()
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

    @property
    def destinations(self) -> str:
        """The remote's push URLs, as one phrase ("a", "a and b")."""
        return _join_words(list(self.remote_urls))

    @property
    def remote_key(self) -> list[str]:
        """The URLs a saved answer about the remote is for: every push URL, sorted.
        Adding, removing or changing one makes the answer stale, and so does a
        different remote (the answer records ``remote`` beside them)."""
        return sorted(self.remote_urls)


# Up to this many files are named on the command line; more go in a list file.
_INLINE_FILES = 10


def untrack_commands(root: Path, git: GitState) -> list[str]:
    """``git rm --cached`` for exactly the tracked day-log files: a dry run, then the real one.

    Never a directory or a pattern: each file is named, on the command line
    or in a list file the user can read first (``--pathspec-from-file``).
    ``--cached`` leaves the files on disk.
    """
    base = f'git -C "{root}" rm --cached --quiet'
    if len(git.tracked_files) <= _INLINE_FILES:
        files = " ".join(git.tracked_files)
        return [f"{base} --dry-run -- {files}", f"{base} -- {files}"]
    # Outside the tree, so no backup commits the list itself.
    tag = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:8]
    listing = Path(tempfile.gettempdir()) / f"smriti-day-log-to-untrack-{tag}.txt"
    listing.write_text("\n".join(git.tracked_files) + "\n", encoding="utf-8")
    spec = f'--pathspec-from-file="{listing}"'
    return [f"{base} --dry-run {spec}", f"{base} {spec}"]


def _shown(files: tuple[str, ...]) -> list[str]:
    """Up to ``_INLINE_FILES`` names, then how many more."""
    shown = list(files[:_INLINE_FILES])
    if len(files) > _INLINE_FILES:
        shown.append(f"... and {len(files) - _INLINE_FILES} more (`git status -- log/` lists them)")
    return shown


def _join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


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

    Only the ``capture`` and ``sources`` keys are touched, and the file is
    created if needed. On sets ``"capture": true``, so the default sources
    apply, and removes an empty ``sources``; a list of the file's own
    sources is left alone, as it captures already. Off sets ``"sources": []``,
    which every version of smriti reads as nothing (a logd still running
    older code included, which would take a missing file or a missing
    ``sources`` for the defaults), and drops ``"capture": true``.
    ``extra_sources`` is never touched, so its sources are still captured
    either way.
    """
    path = daylog_path(root)
    data = read_daylog(root) or {}
    sources = data.get("sources")
    changes: list[str] = []
    if enabled:
        if isinstance(sources, list) and sources:
            # The file's own sources stay; but "capture": false outranks them,
            # so on must clear it (Codex review of H11.1).
            if data.get("capture") is False:
                del data["capture"]
                _write_json(path, data)
                return f'removed "capture": false in {path}, so its own sources apply'
            return None
        if sources == []:
            del data["sources"]
            changes.append('removed "sources": []')
        if data.get("capture") is not True:
            data["capture"] = True
            changes.append('set "capture": true')
        if not changes:
            return None
        _write_json(path, data)
        return f"{' and '.join(changes)} in {path}, so the default sources apply"
    if sources != []:
        data["sources"] = []
        changes.append('set "sources": []')
    if data.get("capture") is True:
        del data["capture"]
        changes.append('removed "capture": true')
    if not changes:
        return None
    _write_json(path, data)
    return f"{' and '.join(changes)} in {path}"


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
    return _join_words(labels)


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


def _names_a_location(remote: str) -> bool:
    """A URL or a path (``host:path``, ``scheme://``, a directory), not a remote's name."""
    return any(c in remote for c in ":/\\")


def _url_rewrites(root: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """git's URL rewrites, ``(insteadOf, pushInsteadOf)``: each base with its prefixes.

    Bases keep the order of their first appearance in the config, as git's
    own list of rewrites does; that order breaks ties between equal prefixes.
    """
    done = _git(root, "config", "-z", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$")
    if done.returncode not in (0, 1):  # 1: none set
        raise subprocess.SubprocessError(done.stderr.strip() or "git config failed")
    fetch: dict[str, list[str]] = {}
    push: dict[str, list[str]] = {}
    for entry in done.stdout.split("\0"):
        key, newline, value = entry.partition("\n")  # -z: key, newline, value
        if not newline:
            continue  # no value: git refuses such a rewrite (and the push with it)
        base, _, var = key[len("url."):].rpartition(".")
        (push if var == "pushinsteadof" else fetch).setdefault(base, []).append(value)
    return fetch, push


def _rewrite(url: str, rewrites: dict[str, list[str]]) -> str | None:
    """``url`` rewritten by the longest matching prefix, or None when none matches.

    git's ``alias_url``: the first of equally long prefixes wins.
    """
    best: tuple[int, str] | None = None
    for base, prefixes in rewrites.items():
        for prefix in prefixes:
            if url.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), base)
    return None if best is None else best[1] + url[best[0]:]


def _direct_push_url(root: Path, url: str) -> str:
    """Where ``git push <url>`` really sends, for a URL that names no remote.

    As git works it out (``url.<base>.pushInsteadOf`` and ``.insteadOf`` in
    git-config(1)): a matching pushInsteadOf rewrites the URL as given and
    wins; otherwise a matching insteadOf does; within each, the longest
    matching prefix. A named remote needs none of this: ``git remote get-url
    --push`` applies the same rules itself.
    """
    fetch, push = _url_rewrites(root)
    for rewrites in (push, fetch):
        rewritten = _rewrite(url, rewrites)
        if rewritten is not None:
            return rewritten
    return url


def backup_remote(root: Path) -> tuple[str | None, str]:
    """The remote ``backup.py`` pushes to, or (None, why) when it pushes nothing.

    The rule of ``push_target`` in ``narada/.smriti/backup.py``, which is a
    standalone script and can't import this, so the two are copies that a
    test holds together: the current branch's upstream remote when it has
    an upstream (``branch.<b>.remote`` and ``branch.<b>.merge``), otherwise
    ``origin``; a detached HEAD isn't pushed. Raises SubprocessError when
    git can't say what HEAD is.
    """
    head = _git(root, "symbolic-ref", "-q", "HEAD")
    ref = head.stdout.strip()
    if head.returncode == 1:
        return None, "HEAD is detached"
    if head.returncode != 0:
        raise subprocess.SubprocessError(head.stderr.strip() or "git symbolic-ref failed")
    if not ref.startswith("refs/heads/"):
        return None, f"HEAD is on {ref}, not a branch"
    branch = ref[len("refs/heads/"):]
    remote = _git(root, "config", "--get", f"branch.{branch}.remote")
    merge = _git(root, "config", "--get", f"branch.{branch}.merge")
    if (remote.returncode == 0 and merge.returncode == 0
            and remote.stdout.strip() and merge.stdout.strip()):
        return remote.stdout.strip(), ""
    return "origin", ""


def _push_urls(root: Path, remote: str) -> list[str]:
    """Every URL ``git push <remote>`` sends to, credentials removed; [] when none."""
    # --all: with several push URLs, `git push <remote>` pushes to each.
    url = _git(root, "remote", "get-url", "--push", "--all", remote)
    if url.returncode == 0:
        lines = url.stdout.splitlines()
    elif "no such remote" not in url.stderr.lower():
        raise subprocess.SubprocessError(url.stderr.strip() or f"git exited {url.returncode}")
    elif _names_a_location(remote):
        # An upstream that names a URL or path directly: git pushes there, rewritten.
        lines = [_direct_push_url(root, remote)]
    else:
        lines = []
    urls: list[str] = []
    for line in lines:
        redacted = redact_url(line.strip())
        if redacted and redacted not in urls:
            urls.append(redacted)
    return urls


def inspect_git(root: Path) -> GitState:
    """The repository, the remote ``backup.py`` pushes to (:func:`backup_remote`)
    and its push URLs, and how git treats the day-log: which of it is
    committed, and whether git-crypt is asked to encrypt it."""
    if not (root / ".git").exists():  # backup.py's own test for a repository
        return GitState(is_repo=False)
    try:
        remote, no_push = backup_remote(root)
        urls = _push_urls(root, remote) if remote is not None else []
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
    return GitState(
        is_repo=True,
        remote_urls=tuple(urls),
        remote=redact_url(remote) if remote is not None else "origin",
        no_push=no_push,
        log_ignored=log_ignored,
        log_tracked=len(tracked),
        log_untracked=len(untracked),
        tracked_files=tuple(sorted(tracked)),
        untracked_files=tuple(sorted(untracked)),
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
    if git.no_push:
        return "\n" + _para(
            f"Backup remote: in {root}, {git.no_push}, so the backup commits on this machine "
            "and pushes nothing. Skipping this question. Once a branch is checked out, the "
            "backup pushes it to its upstream (or to origin); run the installer again then."
        )
    if not git.remote_urls:
        return "\n" + _para(
            f"Backup remote: {root} has no remote named {git.remote}, so the backup commits "
            "on this machine and pushes nothing. Skipping this question."
        )
    if git.log_encrypted:
        crypt = "here it encrypts log/, but file names and commit messages stay readable"
    elif git.crypt_detail:
        crypt = f"here {git.crypt_detail}"
    else:
        crypt = "here it isn't set up for log/"
    if _names_a_location(git.remote):  # the upstream names the URL itself
        where = git.destinations
        if git.remote not in git.remote_urls:
            where += (f" (the branch's upstream names {git.remote}, which git's url "
                      "rewriting, pushInsteadOf or insteadOf, sends there)")
    elif len(git.remote_urls) == 1:
        where = f"{git.remote}, {git.destinations}"
    else:
        where = f"every push URL of {git.remote}: {git.destinations}"
    return "\n" + _para(
        f"Backup remote: the hourly backup and the nightly push the memory tree to "
        f"{where}. Git sends files in plaintext unless git-crypt "
        f"encrypts them, and smriti doesn't set git-crypt up ({crypt}). See {privacy_doc()}."
    )


def remote_question(git: GitState) -> str:
    if len(git.remote_urls) == 1:
        return REMOTE_QUESTION.format(url=git.remote_urls[0])
    return REMOTE_QUESTION_MANY.format(urls=git.destinations)


def git_crypt_steps(root: Path, remote: str = "origin") -> str:
    """How to keep the tree unreadable where the backup pushes it (``remote``).

    The backup pushes the branch to its upstream and falls back to ``origin``
    when the branch has none, so stopping it means removing or repointing
    every remote it can reach: unsetting the upstream, or removing the
    upstream's remote, alone only sends it to origin.
    """
    git = f'git -C "{root}"'
    if remote == "origin":
        stop = [
            "    - or stop the backup pushing there: remove origin, or repoint it at a",
            "      private repository (git remote set-url origin <url>):",
            f"        {git} remote remove origin",
        ]
    else:
        direct = _names_a_location(remote)
        upstream = (f"the branch's upstream, which names {remote} directly" if direct
                    else f"{remote}, the branch's upstream remote")
        stop = [
            "    - or stop the backup pushing there: remove or repoint (git remote set-url)",
            f"      every remote it can push to: {upstream},",
            "      and origin, which the backup falls back to when the branch has no",
            "      upstream. Unsetting the upstream, or removing its remote, alone only",
            "      sends the backup to origin:",
            f"        {git} branch --unset-upstream" if direct
            else f"        {git} remote remove {remote}",
            f"        {git} remote remove origin      (if there is one)",
        ]
    return "\n".join([
        "  To keep the tree unreadable there, before backups run, do one of these:",
        "    - make every repository it is pushed to (above) private;",
        *stop,
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
    daylog = read_daylog(root)
    record = read_record(root)
    existing = is_existing_install(root)
    config = load_config(root)
    current = bool(config.sources)
    git = inspect_git(root)
    notices: list[str] = []

    choice: Choice | None = None
    answered = _answer(record, "capture")
    if capture is not None:
        choice = Choice(capture, FLAG)
    elif (answered is not None and answered.get("enabled") is True
          and (daylog is None or not {"capture", "sources"} & daylog.keys())):
        # Answered yes when a daylog.json without "capture" or "sources" (or
        # none at all) meant the default sources; since H11.1 it means
        # nothing (or only extra_sources), so the recorded answer is written out.
        choice = Choice(True, RECORDED)
        notices.append(
            'capture is on, as you answered before (.smriti/privacy.json): .smriti/daylog.json '
            'now says "capture": true, which capture needs since it stopped being the default.'
        )
    elif answered is not None:
        choice = Choice(current, KEPT)  # chosen before; daylog.json holds it
    elif interactive:
        print(capture_explanation(root))
        answer = ask_yes_no(CAPTURE_QUESTION, current if existing else None, read)
        if answer is not None:
            choice = Choice(answer, PROMPT)
    if choice is None and existing:
        choice = Choice(current, KEPT)
        why = ".smriti/daylog.json says so" if current else config.capture_off
        notices.append(
            f"capture is {'on' if current else 'OFF'} because {why}; nobody has chosen it "
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
    if not git.remote_urls:
        remote = Choice(None, SKIPPED)
        if interactive:
            print(remote_explanation(root, git))
        if remote_private is not None:
            notices.append("--remote-is-private/--remote-not-private ignored: there is no remote.")
    elif remote_private is not None:
        remote = Choice(remote_private, FLAG)
    elif (
        recorded is not None
        and recorded.get("urls") == git.remote_key
        # Answers recorded before the backup followed the upstream were about origin.
        and recorded.get("name", "origin") == git.remote
        and isinstance(recorded.get("private"), bool)
    ):
        remote = Choice(recorded["private"], KEPT)
    elif interactive:
        print(remote_explanation(root, git))
        answer = ask_yes_no(remote_question(git), None, read)
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
    if plan.capture.how in (FLAG, PROMPT, DEFAULT, RECORDED):
        enabled = bool(plan.capture.value)
        change = set_capture(root, enabled)
        if change:
            changes.append(change)
        if plan.capture.how != RECORDED:  # that answer is in the record already
            record["capture"] = {"enabled": enabled, "how": plan.capture.how, "at": stamp}
            dirty = True
    if plan.remote.how in _ANSWERED:
        record["remote"] = {
            "name": plan.git.remote,  # the answer covers exactly this remote
            "urls": plan.git.remote_key,  # with exactly these URLs
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
    config = load_config(root)
    sources = config.sources
    git = inspect_git(root)
    what = describe_sources(sources)
    lines = ["", "privacy:"]
    if sources:
        lines.append(f"  captured:  {what}, copied into {root / 'log'} as you talk")
    else:
        lines.append(f"  captured:  nothing ({config.capture_off or 'no sources are configured'})")
    lines.append(
        "             (a running smriti logd rereads .smriti/daylog.json when it changes, "
        "on its next pass, within a few seconds)"
    )

    # Day-log files already tracked, or on disk and not ignored, are
    # committed whether or not capture is on now.
    conversations = (bool(sources) and not git.log_ignored) or git.log_on_disk > 0
    pushes_plain = False
    if git.error or not git.is_repo:
        why = (f"git couldn't be run ({git.error})" if git.error
               else f"{root} isn't a git repository")
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
                "committed earlier are still tracked, and each backup commits their changes. "
                "To untrack exactly those files (they stay on disk, and history keeps them), "
                "check first with --dry-run, then run it without:"
            )
            lines.extend(f"               {cmd}" for cmd in untrack_commands(root, git))
        if not sources and git.log_untracked:
            lines.append(
                f"             capture is off, but {git.log_untracked} day-log file(s) it wrote "
                "earlier are on disk and not yet committed; the next backup commits them. "
                "To keep them out without deleting anything, git-ignore log/; they are:"
            )
            lines.extend(f"               {p}" for p in _shown(git.untracked_files))
        if not git.remote_urls:
            why = git.no_push or f"there is no remote named {git.remote}"
            lines.append(f"  pushed:    nothing; {why}")
        else:
            if git.log_encrypted:
                how = "log/ encrypted by git-crypt, everything else in plaintext"
            elif git.log_crypt == CRYPT_UNVERIFIED:
                how = "in plaintext unless git-crypt encrypts log/ (unverified)"
            else:
                how = "in plaintext"
            many = len(git.remote_urls) > 1
            if plan.remote.value is True:
                said = "all private, you said" if many else "private, you said"
            elif plan.remote.value is False:
                said = "NOT all private, you said" if many else "NOT private, you said"
            else:
                said = "nobody has said " + ("they are all private" if many else "it is private")
            lines.append(f"  pushed:    {tree}, to {git.destinations}, {how}")
            if git.crypt_detail and conversations:
                lines.append(f"  git-crypt: {git.crypt_detail}")
            lines.append(f"  remote:    {said}")
            pushes_plain = plan.remote.value is not True
        lines.append(
            "  (the backup and the nightly run once `smriti tasks install` schedules them)"
        )
    if pushes_plain and conversations and not git.log_encrypted:
        lines.append(
            "  WARNING: your conversations would go in plaintext to a remote nobody has "
            "said is private."
        )
    if pushes_plain:
        lines.append(git_crypt_steps(root, git.remote))
    lines.extend(f"  note: {notice}" for notice in plan.notices)
    return "\n".join(lines)
