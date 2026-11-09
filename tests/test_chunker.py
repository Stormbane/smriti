"""H15.10: no chunk holds a line longer than the chunker's size.

The vendored chunker split a long section at paragraph and line boundaries
only, so a line longer than ``max_chunk_size`` joined a chunk whole: one
24,000-character turn in a conversation became one chunk of about 6,000
tokens, and embedding it beside 31 others padded them all to its length
(LongMemEval, ``docs/BENCHMARKS.md``). Such a line is now cut into pieces at
word boundaries. Everything else chunks as before: the ordinary note under
``tests/fixtures/chunker/`` gives the chunks the old chunker gave it.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from pathlib import Path

import pytest

import smriti.store.indexer as indexer
from smriti._vendored.memsearch.chunker import (
    MAX_CHUNK_SIZE,
    OVERLAP_LINES,
    chunk_markdown,
    max_chunk_chars,
)
from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.embed import provider as embedding

FIXTURES = Path(__file__).parent / "fixtures" / "chunker"
WORDS = ("Sam told Ari about the trip along the coast, the lighthouse and the small "
         "harbour cafe where they planned the next week").split()


def _long_line(chars: int, seed: int = 1) -> str:
    rng = random.Random(seed)
    words: list[str] = []
    length = 0
    while length < chars:
        words.append(rng.choice(WORDS))
        length += len(words[-1]) + 1
    return " ".join(words)[:chars].rstrip()


def _conversation(turn: str) -> str:
    """A day-log render with one long assistant turn between short ones."""
    return ("# Conversation, 2023-05-20 02:21\n\n"
            "- `02:21` **User:** what did we plan for the coast?\n"
            f"- `02:21` **Assistant:** {turn}\n"
            "- `02:22` **User:** thanks, that helps\n")


def test_ordinary_markdown_chunks_as_before() -> None:
    # The expected chunks were recorded with the chunker before H15.10. The
    # note has every kind of section it splits (paragraphs, a list without
    # blank lines, long lines back to back, a line of exactly the size, CJK
    # text), but no line longer than the size, so nothing may change.
    text = (FIXTURES / "ordinary.md").read_text(encoding="utf-8")
    assert max(len(line) for line in text.split("\n")) <= MAX_CHUNK_SIZE
    expected = json.loads((FIXTURES / "ordinary.chunks.json").read_text(encoding="utf-8"))
    got = [[c.start_line, c.end_line, c.heading_level, c.heading, len(c.content),
            c.content_hash] for c in chunk_markdown(text, source="notes/ordinary.md")]
    assert got == expected


def test_a_24000_character_line_becomes_chunks_within_the_size() -> None:
    turn = _long_line(24_000)
    chunks = chunk_markdown(_conversation(turn), source="conversations/2023/05/20.md")
    assert max(len(c.content) for c in chunks) <= MAX_CHUNK_SIZE
    assert len(chunks) >= 24_000 // MAX_CHUNK_SIZE
    # The pieces of the line all name it (line 4), and the turns around it keep theirs.
    assert {(c.start_line, c.end_line) for c in chunks[1:-1]} == {(4, 4)}
    assert chunks[-1].content == "- `02:22` **User:** thanks, that helps"


def test_the_pieces_keep_every_word_whole_and_in_order() -> None:
    turn = _long_line(10_000, seed=7)
    chunks = chunk_markdown(_conversation(turn), source="c.md")
    shown = " ".join(c.content for c in chunks)
    # Words the line doesn't have would mean a word was cut in two.
    vocabulary = set(WORDS) | set("- `02:21` `02:22` **User:** **Assistant:** what did we "
                                  "plan for coast? thanks, that helps # Conversation, "
                                  "2023-05-20 02:21".split())
    assert set(shown.split()) <= vocabulary
    # Nothing is lost or repeated: the turn's words, in order.
    pieces = [c.content for c in chunks if (c.start_line, c.end_line) == (4, 4)]
    first = chunks[0].content.split("**Assistant:** ", 1)[1]
    assert " ".join([first, *pieces]).split() == turn.split()


def test_the_lines_before_a_long_line_start_its_first_piece() -> None:
    line = _long_line(4_000)
    chunks = chunk_markdown("## Plans\n\n" + line, source="n.md")
    # No chunk of the heading alone: it opens the first piece.
    heading, first = chunks[0].content.split("\n\n", 1)
    assert heading == "## Plans"
    assert line.startswith(first) and len(first) > MAX_CHUNK_SIZE // 2
    assert all(len(c.content) <= MAX_CHUNK_SIZE for c in chunks)
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 3)
    assert {(c.start_line, c.end_line) for c in chunks[1:]} == {(3, 3)}


def test_a_word_longer_than_the_size_is_cut_at_the_size() -> None:
    token = "x" * 4_000  # a hash, a URL, text without spaces
    chunks = chunk_markdown(f"## Key\n\nbefore\n{token}\nafter\n", source="k.md")
    pieces = [c.content for c in chunks]
    assert all(len(p) <= MAX_CHUNK_SIZE for p in pieces)
    assert "".join(re.sub(r"[^x]", "", p) for p in pieces) == token


def test_a_cut_prefers_a_sentence_end() -> None:
    sentences = " ".join(f"Ari wrote note number {n} about the harbour." for n in range(400))
    chunks = chunk_markdown(sentences, source="s.md")
    assert len(chunks) > 1
    assert all(c.content.endswith(".") for c in chunks)
    assert all(len(c.content) <= MAX_CHUNK_SIZE for c in chunks)


def test_a_long_cjk_line_is_cut_at_its_full_stops() -> None:
    line = "サムとアリは港の小さな店で来週の計画を立てた。" * 200
    chunks = chunk_markdown(line, source="j.md")
    assert all(len(c.content) <= MAX_CHUNK_SIZE for c in chunks)
    assert all(c.content.endswith("。") for c in chunks)
    assert "".join(c.content for c in chunks) == line


def _random_document(rng: random.Random, size: int) -> str:
    """Headed sections of lines from empty to several times the size; every
    word unique (``a<n>z``, so none is found inside another)."""
    lines: list[str] = []
    counter = 0
    for s in range(rng.randint(1, 4)):
        lines += [f"## Section {s}", ""]
        for _ in range(rng.randint(1, 12)):
            if rng.random() < 0.2:
                lines.append("")
                continue
            target = rng.choice([rng.randint(1, size // 2), rng.randint(size // 2, size),
                                 rng.randint(size, 5 * size)])
            words: list[str] = []
            length = 0
            while length < target:
                words.append(f"a{counter}z")
                length += len(words[-1]) + 1
                counter += 1
            lines.append(" ".join(words))
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("size", [60, 300, MAX_CHUNK_SIZE])
@pytest.mark.parametrize("overlap", [0, 1, OVERLAP_LINES, 3])
def test_no_chunk_exceeds_the_stated_bound(size: int, overlap: int) -> None:
    rng = random.Random(size * 10 + overlap)
    bound = max_chunk_chars(size, overlap)
    for _ in range(40):
        text = _random_document(rng, size)
        chunks = chunk_markdown(text, max_chunk_size=size, overlap_lines=overlap)
        for c in chunks:
            assert len(c.content) <= bound
            if len(c.content) > size:  # only a chunk led by its overlap
                assert c.content.count("\n") <= overlap, c.content
                assert max(len(line) for line in c.content.split("\n")) <= size
        if overlap == 0:
            assert max(len(c.content) for c in chunks) <= size
        # No text is lost. (A section that ends on an overlap longer than the
        # size is still cut at the size wherever it has no sentence end, as
        # upstream cuts it, so a word there can be split across two chunks.)
        joined = re.sub(r"\s", "", "".join(c.content for c in chunks))
        assert set(re.findall(r"a\d+z", text)) <= set(re.findall(r"a\d+z", joined))
        # The words of a line longer than the size are never cut.
        whole = {w for c in chunks for w in c.content.split()}
        assert all(w in whole for line in text.split("\n") if len(line) > size
                   for w in line.split())


def test_the_default_bound() -> None:
    assert max_chunk_chars() == (OVERLAP_LINES + 1) * MAX_CHUNK_SIZE + OVERLAP_LINES


def test_a_file_with_one_long_line_indexes_into_chunks_within_the_size(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "tree"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "pasted.md").write_text(_conversation(_long_line(24_000)),
                                              encoding="utf-8")
    calls: list[list[str]] = []

    class Recording(StubEmbedding):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls.append(texts)
            return await super().embed(texts)

    monkeypatch.setattr(embedding, "embedder", lambda: Recording())
    db = tmp_path / "index.db"
    stats = indexer.index_tree(root=root, db=db)
    conn = sqlite3.connect(db)
    try:
        lengths = [n for (n,) in conn.execute("SELECT length(content) FROM chunks")]
    finally:
        conn.close()
    assert stats["chunks"] == len(lengths) >= 16
    assert max(lengths) <= MAX_CHUNK_SIZE
    assert max(len(t) for batch in calls for t in batch) <= MAX_CHUNK_SIZE
