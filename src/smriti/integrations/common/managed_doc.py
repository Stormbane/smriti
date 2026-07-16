"""Managed-block agent docs: markers, validation, migration, atomic writes.

Harness agent docs (``~/.claude/CLAUDE.md``, ``~/.codex/AGENTS.md``) are
shared real estate: smriti generates the memory contract, the user adds
personal sections. Before this module, installers overwrote the whole
file — destroying user content on every upgrade.

The contract now: smriti owns exactly one marked block per file,

    <!-- smriti:managed:begin -->
    ...generated contract...
    <!-- smriti:managed:end -->

and never touches a byte outside it. Files without markers are
*fail-closed*: the installer refuses before mutating anything else and
tells the user to run the one-time migration, which recognizes legacy
generated sections by their heading set and replaces them with a marked
block, preserving everything else byte-for-byte.

All writes go through a temp file + ``os.replace`` so an interrupted
write can never leave a partial doc.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

MANAGED_BEGIN = "<!-- smriti:managed:begin -->"
MANAGED_END = "<!-- smriti:managed:end -->"

# Every H2 title smriti has ever generated into an agent doc, across
# template versions. Migration removes sections with these exact titles
# (including their ### children) and replaces the first with the marked
# block. Titles are matched on the text after "## ", stripped.
LEGACY_SECTION_TITLES = frozenset({
    # current template (templates/AGENT.md)
    "Memory system — smriti is the single write path",
    "Memory search — when to call `smriti_read`",
    "Wake briefing",
    "Tools you have",
    # older template generations still live in deployed docs
    "Memory search — prefer smriti_read over Grep",
    "Session wake",
    # harness addenda, both generations
    "Session wake (Claude Code)",
    "Session wake (Codex CLI)",
    "AGENTS.md precedence",
    "Memory search — ambient recall is wired",
})


class ManagedDocError(Exception):
    """Raised when an agent doc cannot be safely written."""


@dataclass(frozen=True)
class DocState:
    """Classification of an on-disk agent doc."""

    kind: str  # "missing" | "managed" | "migration-required" | "malformed"
    detail: str


def render_managed_block(content: str) -> str:
    """Wrap generated contract content in the marker pair."""
    return f"{MANAGED_BEGIN}\n{content.rstrip()}\n{MANAGED_END}"


def classify_doc(path: Path) -> DocState:
    """Classify ``path`` without modifying it.

    ``missing``            → fresh install may create it (with markers).
    ``managed``            → exactly one well-ordered marker pair; safe
                             to rewrite the block in place.
    ``migration-required`` → file exists with no markers (the pre-marker
                             install format, or a hand-written file).
    ``malformed``          → duplicate or reversed markers; never write.
    """
    if not path.exists():
        return DocState("missing", "file does not exist")
    text = path.read_text(encoding="utf-8")
    begins = text.count(MANAGED_BEGIN)
    ends = text.count(MANAGED_END)
    if begins == 0 and ends == 0:
        return DocState(
            "migration-required",
            f"{path} has no smriti:managed markers — run install with "
            "--migrate-agent-doc once to convert it",
        )
    if begins != 1 or ends != 1:
        return DocState(
            "malformed",
            f"{path} has {begins} begin / {ends} end markers; expected "
            "exactly one pair — fix the file by hand",
        )
    if text.index(MANAGED_BEGIN) > text.index(MANAGED_END):
        return DocState("malformed", f"{path} has reversed markers")
    return DocState("managed", "exactly one marker pair")


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp file + os.replace so interruption never truncates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_managed_doc(path: Path, block_content: str, *, title: str | None = None) -> bool:
    """Create or rewrite the managed block; user content is untouchable.

    ``title`` is the H1 line used only when creating a missing file —
    the title belongs to the user's document, not to the block, so an
    existing doc always keeps its own.

    Returns True if the file changed, False if already up to date.
    Raises ManagedDocError on migration-required or malformed docs —
    callers run this AFTER :func:`classify_doc` preflight, so hitting
    the raise means a caller skipped preflight; fail loudly either way.
    """
    state = classify_doc(path)
    block = render_managed_block(block_content)
    if state.kind == "missing":
        head = f"{title}\n\n" if title else ""
        _atomic_write(path, head + block + "\n")
        return True
    if state.kind in ("migration-required", "malformed"):
        raise ManagedDocError(state.detail)
    text = path.read_text(encoding="utf-8")
    start = text.index(MANAGED_BEGIN)
    end = text.index(MANAGED_END) + len(MANAGED_END)
    new_text = text[:start] + block + text[end:]
    if new_text == text:
        return False
    _atomic_write(path, new_text)
    return True


_H2_RE = re.compile(r"^## +(.*?)\s*$", re.MULTILINE)


def migrate_agent_doc(path: Path, block_content: str) -> Path:
    """One-time conversion of a legacy (unmarked) doc to the marked form.

    Removes every H2 section whose title is in LEGACY_SECTION_TITLES
    (heading through the line before the next H2, so ### children go
    with their parent), inserts the marked block at the position of the
    first removed section, and preserves all other content byte-for-byte.
    If no legacy section is found the block is appended.

    A ``<name>.pre-migrate.bak`` backup is written first. Returns the
    backup path. Raises ManagedDocError if the doc is already managed
    or malformed (never double-migrate, never touch a malformed file).
    """
    state = classify_doc(path)
    if state.kind == "missing":
        raise ManagedDocError(f"{path} does not exist — nothing to migrate")
    if state.kind == "managed":
        raise ManagedDocError(f"{path} already has managed markers")
    if state.kind == "malformed":
        raise ManagedDocError(state.detail)

    text = path.read_text(encoding="utf-8")
    matches = list(_H2_RE.finditer(text))

    # Section span: heading start → next H2 start (or EOF).
    spans_to_remove: list[tuple[int, int]] = []
    for i, m in enumerate(matches):
        if m.group(1).strip() in LEGACY_SECTION_TITLES:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            spans_to_remove.append((m.start(), end))

    block = render_managed_block(block_content)
    if spans_to_remove:
        insert_at = spans_to_remove[0][0]
        out: list[str] = []
        cursor = 0
        for start, end in spans_to_remove:
            out.append(text[cursor:start])
            if start == insert_at:
                out.append(block + "\n\n")
            cursor = end
        out.append(text[cursor:])
        new_text = "".join(out)
    else:
        new_text = text.rstrip() + "\n\n" + block + "\n"

    backup = path.with_name(path.name + ".pre-migrate.bak")
    shutil.copy2(path, backup)
    _atomic_write(path, new_text)
    return backup


__all__ = [
    "MANAGED_BEGIN",
    "MANAGED_END",
    "LEGACY_SECTION_TITLES",
    "ManagedDocError",
    "DocState",
    "classify_doc",
    "render_managed_block",
    "write_managed_doc",
    "migrate_agent_doc",
]
