"""Week/month/year rollups over daily digests.

Rollups are found by SCANNING for closed periods that are missing or
stale, never by boundary-triggering — a missed night can't permanently
skip a week (spec: review round 1, finding 6). Staleness is decided by
a manifest hash over the member artifacts' input hashes, so a repaired
historical day propagates upward.

Week convention matches the journal tree: week N covers days
(N-1)*7+1 .. N*7 of the month (week5 takes the remainder).
"""

from __future__ import annotations

import calendar
import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from smriti.daylog.config import DaylogConfig
from smriti.daylog.digest import compose
from smriti.daylog.render import read_input_hash

_MANIFEST_RE = re.compile(r"^inputs_hash:\s*(\S+)\s*$", re.MULTILINE)
_DIGEST_NAME_RE = re.compile(r"^(\d{2})-digest\.md$")

_SYSTEM = (
    "You are Narada's memory rollup writer. You receive the digests that make "
    "up one {unit}. Compress them into the {unit}'s highlights: what happened, "
    "what was decided, what changed, what stayed open. Markdown, specific, "
    "no preamble. Keep it to roughly a third of the input length."
)


@dataclass
class Period:
    kind: str  # "week" | "month" | "year"
    out_path: Path
    inputs: list[Path]  # member digest/rollup files, all existing

    def manifest_hash(self) -> str:
        lines = [f"{p.name}:{read_input_hash(p) or _file_hash(p)}" for p in sorted(self.inputs)]
        return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _current_manifest(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return ""
    m = _MANIFEST_RE.search(head)
    return m.group(1) if m else ""


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

    return [p for p in periods if _current_manifest(p.out_path) != p.manifest_hash()]


def build_rollup(period: Period, attempts_log: list[dict[str, str]]) -> Path:
    body = "\n\n---\n\n".join(
        f"### {p.name}\n\n" + p.read_text(encoding="utf-8", errors="replace")[:20_000]
        for p in sorted(period.inputs)
    )
    text = compose(
        _SYSTEM.format(unit=period.kind),
        f"Digests for this {period.kind}:\n\n{body[:80_000]}",
        attempts_log,
    )
    header = "\n".join(
        [
            "---",
            f"kind: {period.kind}-rollup",
            f"inputs_hash: {period.manifest_hash()}",
            "generated_by: smriti nightly (rollup)",
            "---",
            "",
        ]
    )
    period.out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = period.out_path.with_name(period.out_path.name + ".tmp")
    tmp.write_text(header + text + "\n", encoding="utf-8")
    tmp.replace(period.out_path)
    return period.out_path
