"""Day-log configuration: paths and the source roster.

Sources are config-driven watch globs (spec constraint): new transcript
directories — e.g. the phone-app -> prana brain server work — are a
config edit, never a code change. Overrides live in
``<tree>/.smriti/daylog.json``:

    {"sources": [{"name": "...", "kind": "claude_jsonl", "glob": "..."}],
     "notify_cmd": ["...", "..."],
     "user_name": "Sam", "agent_name": "Ari"}

``sources`` REPLACES the defaults when present; ``extra_sources``
appends to them. ``kind`` selects the adapter module.

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

    def speaker(self, who: str) -> str:
        """The display name for a turn's role; anything but the user is the agent."""
        return self.user_name if who == USER else self.agent_name

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


def load_config(root: Path | None = None) -> DaylogConfig:
    root = root or tree_root()
    cfg = DaylogConfig(root=root, sources=default_sources())
    override_path = root / ".smriti" / "daylog.json"
    try:
        raw = json.loads(override_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg

    def _parse(items: object) -> list[SourceSpec]:
        specs: list[SourceSpec] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name, kind, pattern = item.get("name"), item.get("kind"), item.get("glob")
                if isinstance(name, str) and isinstance(kind, str) and isinstance(pattern, str):
                    specs.append(SourceSpec(name=name, kind=kind, glob=pattern))
        return specs

    # Key presence, not truthiness: an explicit "sources": [] disables
    # every default source (diff review P2).
    if isinstance(raw.get("sources"), list):
        cfg.sources = _parse(raw.get("sources"))
    cfg.sources.extend(_parse(raw.get("extra_sources")))
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
