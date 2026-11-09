"""H15.11: smriti_read shows a whole chunk.

It cut every result at 1,000 characters, so on LongMemEval an agent saw
three items of a list in a chunk of four and said the fourth was cut off.
The limit is now the longest chunk the chunker makes
(``max_chunk_chars``): a chunk of its size, or one led by the lines it
repeats from the chunk before. Only a longer chunk, from an index built
before H15.10, is cut, and marked so.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from smriti._vendored.memsearch.chunker import MAX_CHUNK_SIZE, chunk_markdown, max_chunk_chars
from smriti.mcp.tools import memory
from smriti.store.search import SearchOutcome, SearchResult

MARK = "... (truncated)"


def _read(monkeypatch: pytest.MonkeyPatch, *contents: str) -> str:
    results = [SearchResult(source=f"notes/n{i}.md", heading="Plans", content=c, score=0.9,
                            trunk_distance=2, chunk_id=f"c{i}") for i, c in enumerate(contents)]
    seen: dict[str, Any] = {}

    def search(conn: object, query: str, **kwargs: Any) -> SearchOutcome:
        seen.update(kwargs)
        return SearchOutcome(results)

    # The module: smriti.store re-exports a function under the name "search".
    search_module = importlib.import_module("smriti.store.search")
    monkeypatch.setattr(memory, "_get_db", lambda: SimpleNamespace())
    monkeypatch.setattr(search_module, "search_with_status", search)
    reply = memory.handle_read({"query": "the harbour plans"})
    assert seen["top_k"] == 5
    return reply


def _items(chars: int) -> str:
    """A numbered list of about *chars* characters, its last item at the end."""
    lines = [f"{n}. Ari and Sam planned stop {n} along the coast, by the harbour." for n in
             range(1, 200)]
    text = ""
    for line in lines:
        if len(text) + len(line) + 1 > chars:
            break
        text += line + "\n"
    return text.strip()


def test_a_chunk_of_1400_characters_is_shown_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    chunk = _items(1_400)
    assert 1_300 < len(chunk) <= 1_400
    reply = _read(monkeypatch, chunk)
    assert chunk in reply and MARK not in reply
    assert reply.rstrip("\n").splitlines()[-1] == chunk.splitlines()[-1]  # the last item


def test_the_longest_chunk_the_chunker_makes_is_shown_whole(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Two lines repeated from the chunk before, and one that didn't fit beside them.
    lines = ["a" * MAX_CHUNK_SIZE, "b" * MAX_CHUNK_SIZE, "c" * MAX_CHUNK_SIZE]
    longest = "\n".join(lines)
    assert len(longest) == max_chunk_chars() == memory.preview_chars()
    reply = _read(monkeypatch, longest)
    assert longest in reply and MARK not in reply


def test_a_longer_chunk_is_cut_and_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    old = "x " * 6_000  # one chunk of a long line, from an index built before H15.10
    reply = _read(monkeypatch, old, _items(1_000))
    shown = reply.split("\n", 1)[1].split(f"\n{MARK}\n", 1)[0]
    assert shown == old.strip()[:memory.preview_chars()]
    assert reply.count(MARK) == 1  # the second result is whole
    assert _items(1_000) in reply


def test_the_preview_follows_the_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    # Whatever the chunker makes of a note, smriti_read shows it whole.
    note = "## Coast\n\n" + "\n".join(["Ari wrote a long line about the coast. " * 30] * 6)
    for chunk in chunk_markdown(note, source="notes/coast.md"):
        reply = _read(monkeypatch, chunk.content)
        assert chunk.content in reply and MARK not in reply
