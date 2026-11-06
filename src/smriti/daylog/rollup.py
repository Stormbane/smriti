"""Week/month/year rollups over daily digests.

Rollups are found by SCANNING for closed periods that are missing or
stale, never by boundary-triggering — a missed night can't permanently
skip a week (spec: review round 1, finding 6).

Staleness follows what a rollup read (hardening H12.12). Its frontmatter
records ``members_hash``, a manifest of its members' content, taken from
the bytes it read for its call and before that call: any change to a
member, a digest regenerated from the same turns included, makes it
stale, and a regenerated week makes its month stale in turn.

A rollup written before H12.12 has only ``inputs_hash``, a manifest of
its members' input hashes (a file hash for a member that has none, as a
rollup does). It is judged by that rule until it is regenerated for a
reason that rule sees, so the upgrade itself regenerates nothing; from
then on it carries the new field. New rollups write ``inputs_hash`` too,
so an older smriti still reads them as current.

Week convention matches the journal tree: week N covers days
(N-1)*7+1 .. N*7 of the month (week5 takes the remainder).
"""

from __future__ import annotations

import calendar
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from smriti.core.memwrite import write_memory
from smriti.daylog.config import DEFAULT_AGENT_NAME, DaylogConfig
from smriti.daylog.digest import compose
from smriti.daylog.render import head_field, head_of, input_hash_of, read_head

_DIGEST_NAME_RE = re.compile(r"^(\d{2})-digest\.md$")
_INPUTS_FIELD = "inputs_hash"  # the members' input hashes: the rule before H12.12
_MEMBERS_FIELD = "members_hash"  # the members' content

_SYSTEM = (
    "You are {agent}'s memory rollup writer. You receive the digests that make "
    "up one {unit}. Compress them into the {unit}'s highlights: what happened, "
    "what was decided, what changed, what stayed open. Markdown, specific, "
    "no preamble. Keep it to roughly a third of the input length."
)


def _system_prompt(unit: str, cfg: DaylogConfig | None = None) -> str:
    """The rollup prompt for a week, month or year, naming the configured agent."""
    agent = cfg.agent_name if cfg is not None else DEFAULT_AGENT_NAME
    return _SYSTEM.format(unit=unit, agent=agent)


def _short_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


@dataclass(frozen=True)
class _Member:
    """A member digest or rollup, read once: what its rollup's body and manifests come from."""

    path: Path
    data: bytes | None  # None: it couldn't be read

    @property
    def text(self) -> str:
        """Its text as ``read_text`` gives it."""
        raw = (self.data or b"").decode("utf-8", errors="replace")
        return raw.replace("\r\n", "\n").replace("\r", "\n")

    def content_hash(self) -> str:
        """Its content's hash; line endings don't count."""
        return "" if self.data is None else _short_hash(self.data.replace(b"\r\n", b"\n"))

    def input_hash(self) -> str:
        """The rule before H12.12: the input hash it records, else its file's hash."""
        if self.data is None:
            return ""
        return input_hash_of(head_of(self.data[:8192])) or _short_hash(self.data)


def _read_members(paths: Iterable[Path], *, strict: bool = False) -> list[_Member]:
    """Each member's bytes, in name order; with *strict*, an unreadable one raises OSError."""
    members = []
    for path in sorted(paths):
        try:
            data: bytes | None = path.read_bytes()
        except OSError:
            if strict:
                raise
            data = None
        members.append(_Member(path, data))
    return members


def _manifest(lines: Iterable[str]) -> str:
    return _short_hash("\n".join(lines).encode("utf-8"))


def _input_manifest(members: list[_Member]) -> str:
    """``inputs_hash``: the manifest of the members' input hashes (the rule before H12.12)."""
    return _manifest(f"{m.path.name}:{m.input_hash()}" for m in members)


def _content_manifest(members: list[_Member]) -> str:
    """``members_hash``: the manifest of the members' content."""
    return _manifest(f"{m.path.name}:{m.content_hash()}" for m in members)


@dataclass
class Period:
    kind: str  # "week" | "month" | "year"
    out_path: Path
    inputs: list[Path]  # member digest/rollup files, all existing

    def manifest_hash(self) -> str:
        """The manifest of the members' input hashes: ``inputs_hash``, the rule before H12.12."""
        return _input_manifest(_read_members(self.inputs))

    def is_current(self) -> bool:
        """Whether the rollup exists and its members are what it read."""
        head = read_head(self.out_path)
        if not head:
            return False
        members = _read_members(self.inputs)
        recorded = head_field(head, _MEMBERS_FIELD)
        if recorded:
            return recorded == _content_manifest(members)
        # Written before H12.12: its own rule, until it is regenerated.
        return head_field(head, _INPUTS_FIELD) == _input_manifest(members)


def week_of(day: int) -> int:
    return min((day - 1) // 7 + 1, 5)


def _week_days(year: int, month: int, week: int) -> tuple[int, int]:
    last = calendar.monthrange(year, month)[1]
    start = (week - 1) * 7 + 1
    end = last if week == 5 else min(week * 7, last)
    return start, end


def enumerate_stale_periods(cfg: DaylogConfig, today: date) -> list[Period]:
    """All CLOSED periods whose rollup is missing or stale, oldest first."""
    periods: list[Period] = []
    digests: dict[tuple[int, int], list[Path]] = {}
    for path in sorted(cfg.log_dir.glob("[0-9]" * 4 + "/[0-9][0-9]/*-digest.md")):
        m = _DIGEST_NAME_RE.match(path.name)
        if not m:
            continue
        year, month = int(path.parent.parent.name), int(path.parent.name)
        digests.setdefault((year, month), []).append(path)

    # Weeks and months.
    for (year, month), files in sorted(digests.items()):
        by_week: dict[int, list[Path]] = {}
        for path in files:
            day = int(_DIGEST_NAME_RE.match(path.name).group(1))  # type: ignore[union-attr]
            by_week.setdefault(week_of(day), []).append(path)
        month_dir = cfg.log_dir / f"{year:04d}" / f"{month:02d}"
        week_paths: list[Path] = []
        for week, members in sorted(by_week.items()):
            _start, end = _week_days(year, month, week)
            closed = date(year, month, min(end, calendar.monthrange(year, month)[1])) < today
            out = month_dir / f"week{week}.md"
            if closed:
                periods.append(Period("week", out, members))
            if out.exists():
                week_paths.append(out)
        month_closed = (year, month) < (today.year, today.month)
        if month_closed and week_paths:
            periods.append(Period("month", cfg.log_dir / f"{year:04d}" / f"{month:02d}.md",
                                  week_paths))

    # Years, from existing month rollups.
    by_year: dict[int, list[Path]] = {}
    for path in sorted(cfg.log_dir.glob("[0-9]" * 4 + "/[0-9][0-9].md")):
        by_year.setdefault(int(path.parent.name), []).append(path)
    for year, months in sorted(by_year.items()):
        if year < today.year and months:
            periods.append(Period("year", cfg.log_dir / f"{year:04d}.md", months))

    return [p for p in periods if not p.is_current()]


# Total input budget per rollup kind, in chars. Each input gets an equal share
# (capped at _PER_INPUT_MAX), so a long period never silently drops its tail.
# A flat 80k cut on the joined body meant a year saw only its first ~5 months
# and a long week lost its last days.
_BUDGET = {"week": 80_000, "month": 100_000, "year": 240_000}
_PER_INPUT_MAX = 20_000
_TRUNCATED = "\n\n[... truncated for length]"


def _body(kind: str, members: list[_Member]) -> str:
    share = min(_PER_INPUT_MAX, _BUDGET.get(kind, 80_000) // max(1, len(members)))
    parts = []
    for m in members:
        text = m.text
        if len(text) > share:
            text = text[: share - len(_TRUNCATED)] + _TRUNCATED
        parts.append(f"### {m.path.name}\n\n{text}")
    return "\n\n---\n\n".join(parts)


def rollup_body(period: Period) -> str:
    return _body(period.kind, _read_members(period.inputs, strict=True))


def build_rollup(period: Period, attempts_log: list[dict[str, str]],
                 cfg: DaylogConfig | None = None) -> Path:
    # One read of the members, before the call, for the body and the provenance
    # alike: a member that changes during the call makes this rollup stale.
    members = _read_members(period.inputs, strict=True)
    header = "\n".join(
        [
            "---",
            f"kind: {period.kind}-rollup",
            f"{_INPUTS_FIELD}: {_input_manifest(members)}",
            f"{_MEMBERS_FIELD}: {_content_manifest(members)}",
            "generated_by: smriti nightly (rollup)",
            "---",
            "",
        ]
    )
    text = compose(
        _system_prompt(period.kind, cfg),
        f"Digests for this {period.kind}:\n\n{_body(period.kind, members)}",
        attempts_log,
        cfg=cfg,  # its fallback setting; without cfg, only the environment's
    )
    # Masked, as the digest is: the rollup's model reads digests of untrusted text.
    write_memory(period.out_path, header + text + "\n")
    return period.out_path
