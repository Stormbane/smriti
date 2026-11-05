"""Day-log configuration: paths and the source roster.

Sources are config-driven watch globs (spec constraint): new transcript
directories — e.g. the phone-app -> prana brain server work — are a
config edit, never a code change. Overrides live in
``<tree>/.smriti/daylog.json``:

    {"capture": true,
     "notify_cmd": ["...", "..."],
     "user_name": "Sam", "agent_name": "Ari"}

Capture is off until it is configured (hardening H11.1). Without a
daylog.json, or with one that names no source, nothing is captured; the
same goes for a file that can't be read, isn't JSON or isn't an object,
and :attr:`DaylogConfig.capture_off` says why. ``"capture": true`` turns
on the default sources (:func:`default_sources`: every Claude Code
transcript and Codex rollout in the home directory, and the tree's voice
transcripts). ``sources``, a list of ``{"name", "kind", "glob"}``,
REPLACES them when present, and ``"sources": []`` captures nothing;
``extra_sources`` appends to whichever applies. ``"capture": false``
captures nothing, whatever else the file lists. ``kind`` selects the
adapter module.

``user_name`` and ``agent_name`` are the display names of the two
speaker roles (``smriti.daylog.model.USER`` and ``AGENT``): the day-log
render, the presence line and the nightly prompts use them. They
default to "User" and "Agent".

``profile`` names an optional profile, ``smriti.profiles.<name>``, that
adds one installation's tools, wake sections and morning persona to the
core (see :mod:`smriti.profiles`). Unset, the core runs alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from smriti.core.tree import tree_root
from smriti.daylog.model import USER

DEFAULT_USER_NAME = "User"
DEFAULT_AGENT_NAME = "Agent"


@dataclass(frozen=True)
class SourceSpec:
    name: str
    kind: str  # adapter key: claude_jsonl | codex_rollout | voice_md
    glob: str  # absolute glob pattern


@dataclass
class DaylogConfig:
    root: Path
    sources: list[SourceSpec] = field(default_factory=list)
    notify_cmd: list[str] = field(default_factory=list)
    # Where the user lives, for the morning message's local news (e.g.
    # "Lisbon, Portugal"). Set in the private .smriti/daylog.json, never in code.
    location: str = ""
    # Display names of the two speaker roles.
    user_name: str = DEFAULT_USER_NAME
    agent_name: str = DEFAULT_AGENT_NAME
    # The optional profile (smriti.profiles.<name>); "" means none.
    profile: str = ""
    # Why nothing is captured, when nothing is; "" otherwise (see capture_note).
    capture_off: str = ""

    def speaker(self, who: str) -> str:
        """The display name for a turn's role; anything but the user is the agent."""
        return self.user_name if who == USER else self.agent_name

    @property
    def daylog_path(self) -> Path:
        return self.root / ".smriti" / "daylog.json"

    @property
    def log_dir(self) -> Path:
        return self.root / "log"

    @property
    def state_path(self) -> Path:
        return self.log_dir / ".state.json"

    @property
    def lock_path(self) -> Path:
        return self.log_dir / ".writer.lock"

    @property
    def nightly_status_path(self) -> Path:
        return self.log_dir / ".nightly-status.json"

    @property
    def morning_ledger_dir(self) -> Path:
        return self.log_dir / ".morning-sent"

    def day_jsonl(self, day: str) -> Path:
        y, m, d = day.split("-")
        return self.log_dir / y / m / f"{d}.jsonl"

    def day_md(self, day: str) -> Path:
        return self.day_jsonl(day).with_suffix(".md")

    def day_digest(self, day: str) -> Path:
        y, m, d = day.split("-")
        return self.log_dir / y / m / f"{d}-digest.md"


def default_sources() -> list[SourceSpec]:
    home = Path.home()
    return [
        SourceSpec(
            name="claude",
            kind="claude_jsonl",
            glob=str(home / ".claude" / "projects" / "*" / "*.jsonl"),
        ),
        SourceSpec(
            name="codex",
            kind="codex_rollout",
            glob=str(home / ".codex" / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl"),
        ),
        SourceSpec(
            name="voice",
            kind="voice_md",
            glob=str(tree_root() / "heartbeat" / "voice-transcripts" / "*" / "*.md"),
        ),
    ]


_FILE = ".smriti/daylog.json"


def capture_note(cfg: DaylogConfig) -> str:
    """Capture in a line: the sources it reads, or why it reads none and how to turn it on."""
    if cfg.sources:
        return "on: " + ", ".join(dict.fromkeys(spec.name for spec in cfg.sources))
    why = cfg.capture_off or "no sources are configured"
    return (f"off: {why}. To turn it on, re-run the installer with --capture, or put "
            f'"capture": true (the default sources) or a "sources" list in {cfg.daylog_path}')


def load_config(root: Path | None = None) -> DaylogConfig:
    root = root or tree_root()
    cfg = DaylogConfig(root=root)
    try:
        raw = json.loads(cfg.daylog_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg.capture_off = f"there is no {_FILE}"
        return cfg
    except OSError as exc:
        cfg.capture_off = f"{_FILE} can't be read ({exc.strerror or exc})"
        return cfg
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        cfg.capture_off = f"{_FILE} isn't valid JSON ({exc})"
        return cfg
    if not isinstance(raw, dict):
        cfg.capture_off = f"{_FILE} isn't a JSON object"
        return cfg

    cfg.sources, cfg.capture_off = _sources(raw)
    notify = raw.get("notify_cmd")
    if isinstance(notify, list) and all(isinstance(x, str) for x in notify):
        cfg.notify_cmd = notify
    location = raw.get("location")
    if isinstance(location, str):
        cfg.location = location.strip()
    user_name, agent_name = raw.get("user_name"), raw.get("agent_name")
    if isinstance(user_name, str) and user_name.strip():
        cfg.user_name = user_name.strip()
    if isinstance(agent_name, str) and agent_name.strip():
        cfg.agent_name = agent_name.strip()
    profile = raw.get("profile")
    if isinstance(profile, str):
        cfg.profile = profile.strip()
    return cfg


def _sources(raw: dict[str, object]) -> tuple[list[SourceSpec], str]:
    """What daylog.json captures: the sources, and why there are none when there are none.

    Nothing unless the file says so: capturing every transcript on the
    machine was the default until H11.1, so an install that never chose
    copied them all.
    """
    capture = raw.get("capture")
    if capture is False:
        return [], f'{_FILE} says "capture": false'
    listed = raw.get("sources")
    # Key presence, not truthiness: an explicit "sources": [] disables
    # every default source (diff review P2).
    if isinstance(listed, list):
        sources = _parse(listed)
    elif capture is True:
        sources = default_sources()
    else:
        sources = []
    sources += _parse(raw.get("extra_sources"))
    if sources:
        return sources, ""
    if isinstance(listed, list):
        return [], (f'{_FILE} says "sources": []' if not listed else
                    f'none of the "sources" in {_FILE} has a string "name", "kind" and "glob"')
    if "capture" in raw:
        return [], f'"capture" in {_FILE} is {json.dumps(capture)}, not true'
    return [], f'{_FILE} neither says "capture": true nor lists "sources"'


def _parse(items: object) -> list[SourceSpec]:
    """The well-formed ``{"name", "kind", "glob"}`` entries of a sources list."""
    specs: list[SourceSpec] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name, kind, pattern = item.get("name"), item.get("kind"), item.get("glob")
            if isinstance(name, str) and isinstance(kind, str) and isinstance(pattern, str):
                specs.append(SourceSpec(name=name, kind=kind, glob=pattern))
    return specs
