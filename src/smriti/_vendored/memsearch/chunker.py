"""Markdown chunking — split markdown files into semantic chunks by headings.

---
Vendored from zilliztech/memsearch (MIT License, (c) 2025 Zilliz Inc.)
Upstream: https://github.com/zilliztech/memsearch/blob/main/src/memsearch/chunker.py
Vendored on: 2026-04-12
Modifications from upstream:
  - _split_large_section: a line longer than max_chunk_size is cut into
    pieces at word boundaries (_split_long_line) instead of joining a chunk
    whole, so no chunk holds such a line: upstream let it through, and one
    24,000-character line became one chunk (H15.10). Every other line is
    chunked as upstream does.
  - MAX_CHUNK_SIZE and OVERLAP_LINES name chunk_markdown's defaults, and
    max_chunk_chars says how long a chunk can be with them.
License: see smriti/NOTICE.md and smriti/src/smriti/_vendored/memsearch/LICENSE
---
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# chunk_markdown's defaults (smriti: named, for callers that size what they show).
MAX_CHUNK_SIZE = 1500
OVERLAP_LINES = 2
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Minimum meaningful text length after stripping metadata noise.
# Chunks with less useful text than this are dropped during chunking.
_MIN_MEANINGFUL_LEN = 2


def clean_content_for_embedding(text: str) -> str:
    """Strip metadata noise from chunk content before embedding.

    Removes HTML comments (<!-- ... -->), which often contain session/turn
    UUIDs and transcript paths that dilute embedding quality.  The original
    content stored in Milvus is unchanged — this only affects the text
    sent to the embedding model.
    """
    cleaned = _HTML_COMMENT_RE.sub("", text)
    # Collapse runs of blank lines left behind by removed comments
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _has_meaningful_content(text: str) -> bool:
    """Return True if *text* has enough substance to be worth indexing.

    Strips HTML comments, heading lines, and whitespace, then checks
    whether the remaining body text meets the minimum length threshold.
    A section like ``## Session 03:16`` with no body is rejected, while
    ``## Title\\nSome real content`` is kept.
    """
    # Remove HTML comments first
    stripped = _HTML_COMMENT_RE.sub("", text)
    # Remove heading lines — we only care about body text
    lines = [ln for ln in stripped.splitlines() if not _HEADING_RE.match(ln)]
    body = "\n".join(lines).strip()
    return len(body) >= _MIN_MEANINGFUL_LEN


@dataclass(frozen=True)
class Chunk:
    """A single chunk extracted from a markdown document."""

    content: str
    source: str  # file path
    heading: str  # nearest heading (empty string for preamble)
    heading_level: int  # 0 for preamble
    start_line: int
    end_line: int
    content_hash: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.content_hash:
            h = hashlib.sha256(self.content.encode()).hexdigest()[:16]
            object.__setattr__(self, "content_hash", h)


def compute_chunk_id(
    source: str,
    start_line: int,
    end_line: int,
    content_hash: str,
    model: str,
) -> str:
    """Compute a composite chunk ID matching OpenClaw's format.

    ``hash(source:path:startLine:endLine:contentHash:model)``
    """
    raw = f"markdown:{source}:{start_line}:{end_line}:{content_hash}:{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def max_chunk_chars(
    max_chunk_size: int = MAX_CHUNK_SIZE,
    overlap_lines: int = OVERLAP_LINES,
) -> int:
    """The longest chunk :func:`chunk_markdown` makes with these settings (smriti).

    A chunk is at most *max_chunk_size* characters, with one exception: a
    chunk that starts with the lines it repeats from the chunk before it
    (at most *overlap_lines* of them) and holds the one line that didn't fit
    beside them. That chunk is at most *overlap_lines* + 1 lines, each at
    most *max_chunk_size* characters (a longer line is cut into pieces),
    joined by newlines. With *overlap_lines* 0 no chunk exceeds the size.
    """
    return (overlap_lines + 1) * max_chunk_size + overlap_lines


def chunk_markdown(
    text: str,
    source: str = "",
    *,
    max_chunk_size: int = MAX_CHUNK_SIZE,
    overlap_lines: int = OVERLAP_LINES,
) -> list[Chunk]:
    """Split markdown *text* into chunks, breaking on headings.

    Chunks that exceed *max_chunk_size* characters are split further at
    paragraph boundaries.  A small *overlap_lines* context is carried
    forward to preserve continuity.  A line longer than *max_chunk_size* is
    cut at word boundaries (smriti); :func:`max_chunk_chars` gives the
    longest chunk that can result.
    """
    lines = text.split("\n")
    # Find all heading positions
    heading_positions: list[tuple[int, int, str]] = []  # (line_idx, level, title)
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            heading_positions.append((i, len(m.group(1)), m.group(2).strip()))

    # Build sections between headings
    sections: list[tuple[int, int, str, int]] = []  # (start, end, heading, level)
    if not heading_positions or heading_positions[0][0] > 0:
        end = heading_positions[0][0] if heading_positions else len(lines)
        sections.append((0, end, "", 0))

    for idx, (line_idx, level, title) in enumerate(heading_positions):
        next_start = heading_positions[idx + 1][0] if idx + 1 < len(heading_positions) else len(lines)
        sections.append((line_idx, next_start, title, level))

    chunks: list[Chunk] = []
    for start, end, heading, level in sections:
        section_text = "\n".join(lines[start:end]).strip()
        if not section_text or not _has_meaningful_content(section_text):
            continue

        if len(section_text) <= max_chunk_size:
            chunks.append(
                Chunk(
                    content=section_text,
                    source=source,
                    heading=heading,
                    heading_level=level,
                    start_line=start + 1,
                    end_line=end,
                )
            )
        else:
            # Split large sections at paragraph boundaries
            chunks.extend(
                _split_large_section(
                    lines[start:end],
                    source=source,
                    heading=heading,
                    heading_level=level,
                    base_line=start,
                    max_size=max_chunk_size,
                    overlap=overlap_lines,
                )
            )

    return chunks


def _split_large_section(
    lines: list[str],
    *,
    source: str,
    heading: str,
    heading_level: int,
    base_line: int,
    max_size: int,
    overlap: int,
) -> list[Chunk]:
    """Split a large section into smaller chunks.

    Split priority: paragraph boundary > line boundary > sentence/char boundary.
    """
    chunks: list[Chunk] = []
    current_lines: list[str] = []
    current_start = 0
    carried = 0  # smriti: how many of current_lines the last chunk already holds

    def _emit(content: str, start_line: int, end_line: int) -> None:
        if content:
            chunks.append(
                Chunk(
                    content=content,
                    source=source,
                    heading=heading,
                    heading_level=heading_level,
                    start_line=start_line,
                    end_line=end_line,
                )
            )

    for i, line in enumerate(lines):
        # smriti: a line longer than max_size never joins a chunk whole
        # (upstream added it like any other line, so it became part of a
        # chunk of any size). The lines before it go first: as the start of
        # its first piece when they take at most half of it, else as a
        # chunk of their own (or not at all, when the last chunk already
        # holds them all). Then the line goes in pieces cut at word
        # boundaries, and nothing is carried past it.
        if len(line) > max_size:
            head = "\n".join(current_lines).lstrip()
            room = max_size
            if head.strip() and len(head) + 1 <= max_size // 2:
                room = max_size - len(head) - 1
            else:
                if head.strip() and len(current_lines) > carried:
                    _emit(head.strip(), base_line + current_start + 1, base_line + i)
                head = ""
            for n, part in enumerate(_split_long_line(line, max_size, room)):
                if n == 0 and head:
                    _emit((head + "\n" + part).strip(),
                          base_line + current_start + 1, base_line + i + 1)
                else:
                    _emit(part, base_line + i + 1, base_line + i + 1)
            current_lines = []
            current_start = i + 1
            carried = 0
            continue

        current_lines.append(line)
        text = "\n".join(current_lines)

        is_paragraph_break = line.strip() == "" and i + 1 < len(lines)
        is_last_line = i == len(lines) - 1

        # Preferred: split at paragraph boundary
        if len(text) >= max_size and is_paragraph_break:
            _emit(text.strip(), base_line + current_start + 1, base_line + i + 1)
            overlap_start = max(0, len(current_lines) - overlap)
            current_lines = current_lines[overlap_start:]
            current_start = i + 1 - len(current_lines)
            carried = len(current_lines)
            continue

        # Forced line-boundary split: no paragraph break found but text is
        # too large.  Roll back the current line so the previous lines form
        # a chunk and the current line starts the next one.
        if len(text) >= max_size and not is_paragraph_break and len(current_lines) > 1:
            current_lines.pop()
            content = "\n".join(current_lines).strip()
            _emit(content, base_line + current_start + 1, base_line + i)
            overlap_start = max(0, len(current_lines) - overlap)
            current_lines = current_lines[overlap_start:]
            carried = len(current_lines)
            current_lines.append(line)  # re-add the rolled-back line
            current_start = i - len(current_lines) + 1
            continue

        # Single line exceeds max_size — split within the line
        if len(text) >= max_size and len(current_lines) == 1:
            sub_chunks = _split_long_text(text, max_size)
            for part in sub_chunks:
                _emit(
                    part.strip(),
                    base_line + current_start + 1,
                    base_line + i + 1,
                )
            current_lines = []
            current_start = i + 1
            carried = 0
            continue

        if is_last_line:
            _emit(text.strip(), base_line + current_start + 1, base_line + i + 1)
            current_lines = []

    # Flush any remaining content (e.g. a rolled-back line from the last
    # iteration that never got a chance to be emitted).
    if current_lines:
        remaining = "\n".join(current_lines).strip()
        if remaining:
            end_line = base_line + len(lines)
            start_line = base_line + current_start + 1
            if len(remaining) > max_size:
                for part in _split_long_text(remaining, max_size):
                    _emit(part.strip(), start_line, end_line)
            else:
                _emit(remaining, start_line, end_line)

    return chunks


# Sentence-ending punctuation for splitting long text without line breaks.
_SENTENCE_END_RE = re.compile(r"[。\uFF01\uFF1F.!?]\s*")


def _split_long_text(text: str, max_size: int) -> list[str]:
    """Split a long string that has no line breaks into pieces ≤ *max_size*.

    Prefers splitting at sentence boundaries; falls back to character position.
    """
    parts: list[str] = []
    while len(text) > max_size:
        # Look for the last sentence boundary within max_size
        best = -1
        for m in _SENTENCE_END_RE.finditer(text, 0, max_size):
            best = m.end()
        if best > 0:
            parts.append(text[:best])
            text = text[best:]
        else:
            # No sentence boundary found — hard split at max_size
            parts.append(text[:max_size])
            text = text[max_size:]
    if text:
        parts.append(text)
    return parts


# smriti: where a line longer than the chunk size is cut. A sentence end
# (Latin punctuation before whitespace, or an ideographic full stop,
# exclamation or question mark) is preferred; whitespace will do. Each
# pattern matches up to the LAST such place in the text it is given.
_LAST_SENTENCE_END_RE = re.compile(r"(?s:.*)(?:[.!?](?=\s)|[。！？])")
_LAST_SPACE_RE = re.compile(r"(?s:.*)\s")


def _split_long_line(line: str, max_size: int, first: int) -> list[str]:
    """Cut a line longer than the chunk size into pieces (smriti).

    The first piece is at most *first* characters, every other at most
    *max_size*. A piece ends at the last sentence end in the second half of
    its room, else at the last whitespace in it, so no word is cut; a
    "word" longer than the room (a URL, a hash, text without spaces) is cut
    at the room. The whitespace at a cut belongs to neither piece.
    """
    parts: list[str] = []
    text = line.strip()
    room = max(1, first)
    while len(text) > room:
        # text[room] is in view, so a full stop at room - 1 before a space
        # counts; an ideographic one at room itself would end past the room.
        m = _LAST_SENTENCE_END_RE.match(text, room // 2, room + 1)
        if m and m.end() > room:
            m = _LAST_SENTENCE_END_RE.match(text, room // 2, room)
        if m:
            cut = m.end()
        else:
            m = _LAST_SPACE_RE.match(text, 0, room + 1)
            cut = m.end() - 1 if m and m.end() > 1 else room
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
        room = max_size
    if text:
        parts.append(text)
    return parts
