"""Create and update journal summary files at week/month/year levels.

Journal entries use a cascading time structure:

    journal/YYYY/MM/weekN/MM-DD.md   (daily entries)
    journal/YYYY/MM/weekN/weekN.md   (week summary -- reads daily files)
    journal/YYYY/MM/MM.md            (month summary -- reads week summaries)
    journal/YYYY/YYYY.md             (year summary -- reads month summaries)

Summary files are created by the journal_rollup sleep task when they
don't exist. Once created, they are updated by cognitive cascade when
daily entries change. The cascade stops at each level if the day's
events aren't significant enough to affect the summary at that level.

Each level reads only its children (lossy by design):
- Week reads daily files (most detail)
- Month reads week summaries (compressed)
- Year reads month summaries (most compressed)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from smriti.core.tree import tree_root

log = logging.getLogger(__name__)

WEEK_SUMMARY_PROMPT = """\
Summarize the following daily journal entries into a week summary. \
Focus on the most important events, decisions, insights, and shifts. \
Write in first person as the entity who wrote these entries. \
Keep it concise -- this is a reference summary, not a reproduction. \
Omit routine entries that don't carry forward.

Daily entries:

{entries}
"""

MONTH_SUMMARY_PROMPT = """\
Summarize the following weekly journal summaries into a month summary. \
Focus on the most significant themes, decisions, and developments \
across the month. Write in first person. This should capture what \
mattered at the month level -- the things worth remembering a year \
from now.

Weekly summaries:

{entries}
"""

YEAR_SUMMARY_PROMPT = """\
Summarize the following monthly journal summaries into a year summary. \
Focus on the defining events, major shifts, and lasting developments. \
Write in first person. This is the highest-level view -- what defined \
this year.

Monthly summaries:

{entries}
"""


def _detect_summary_level(rel_path: str) -> str | None:
    """Detect what level of summary a path represents.

    Returns 'week', 'month', 'year', or None.
    """
    parts = rel_path.replace("\\", "/").split("/")
    # journal/YYYY/MM/weekN/weekN.md -> week
    if len(parts) == 5 and parts[3].startswith("week") and parts[4].startswith("week"):
        return "week"
    # journal/YYYY/MM/MM.md -> month
    if len(parts) == 4 and re.match(r"^\d{2}\.md$", parts[3]):
        return "month"
    # journal/YYYY/YYYY.md -> year
    if len(parts) == 3 and re.match(r"^\d{4}\.md$", parts[2]):
        return "year"
    return None


def _collect_children(summary_path: Path, root: Path) -> list[tuple[str, str]]:
    """Collect the child files that a summary should read.

    Returns list of (sort_key, content) tuples, sorted chronologically.
    """
    rel = str(summary_path.relative_to(root)).replace("\\", "/")
    level = _detect_summary_level(rel)
    daily_pattern = re.compile(r"^\d{2}-\d{2}\.md$")
    children: list[tuple[str, str]] = []

    if level == "week":
        # Read daily files in the same weekN directory
        week_dir = summary_path.parent
        for f in sorted(week_dir.glob("*.md")):
            if daily_pattern.match(f.name):
                try:
                    children.append((f.name, f.read_text(encoding="utf-8")))
                except OSError:
                    continue

    elif level == "month":
        # Read week summary files in the month directory
        month_dir = summary_path.parent
        for week_dir in sorted(month_dir.iterdir()):
            if not week_dir.is_dir() or not week_dir.name.startswith("week"):
                continue
            week_summary = week_dir / f"{week_dir.name}.md"
            if week_summary.exists():
                try:
                    children.append((week_dir.name, week_summary.read_text(encoding="utf-8")))
                except OSError:
                    continue

    elif level == "year":
        # Read month summary files in the year directory
        year_dir = summary_path.parent
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or not re.match(r"^\d{2}$", month_dir.name):
                continue
            month_summary = month_dir / f"{month_dir.name}.md"
            if month_summary.exists():
                try:
                    children.append((month_dir.name, month_summary.read_text(encoding="utf-8")))
                except OSError:
                    continue

    return children


def rollup(
    summary_path_str: str,
    root: Path | None = None,
    executor_fn: Any = None,
    dry_run: bool = False,
) -> Path | None:
    """Create a journal summary file by reading its children.

    Parameters
    ----------
    summary_path_str:
        Relative path of the summary to create (e.g. 'journal/2026/04/week3/week3.md')
    root:
        Tree root (defaults to tree_root())
    executor_fn:
        The EXECUTOR function to call for summarization
    dry_run:
        If True, don't actually create the file

    Returns
    -------
    Path or None
        The created file path, or None if skipped/failed.
    """
    if root is None:
        root = tree_root()

    summary_path = root / summary_path_str
    level = _detect_summary_level(summary_path_str.replace("\\", "/"))

    if level is None:
        log.warning("Cannot determine summary level for: %s", summary_path_str)
        return None

    children = _collect_children(summary_path, root)
    if not children:
        log.info("No children found for %s, skipping rollup", summary_path_str)
        return None

    if dry_run:
        log.info("Dry run: would create %s from %d children", summary_path_str, len(children))
        return None

    if executor_fn is None:
        from smriti.store.judge import executor_via_claude
        executor_fn = executor_via_claude

    # Build the prompt
    entries_text = "\n\n".join(
        f"--- {key} ---\n{content}" for key, content in children
    )

    if level == "week":
        prompt = WEEK_SUMMARY_PROMPT.format(entries=entries_text)
    elif level == "month":
        prompt = MONTH_SUMMARY_PROMPT.format(entries=entries_text)
    else:
        prompt = YEAR_SUMMARY_PROMPT.format(entries=entries_text)

    try:
        result, _meta = executor_fn(prompt)
    except Exception as exc:
        log.error("Journal rollup failed for %s: %s", summary_path_str, exc)
        return None

    # Write the summary with frontmatter
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    header = (
        f"---\n"
        f"type: journal-{level}-summary\n"
        f"created: {now.strftime('%Y-%m-%d')}\n"
        f"children: {len(children)}\n"
        f"---\n\n"
        f"# {level.title()} Summary\n\n"
    )

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(header + result.strip() + "\n", encoding="utf-8")
    log.info("Created journal %s summary: %s (%d children)", level, summary_path_str, len(children))

    from smriti.metrics import get_logger
    get_logger().log(
        "journal_rollup",
        level=level,
        path=summary_path_str,
        children=len(children),
    )

    return summary_path
