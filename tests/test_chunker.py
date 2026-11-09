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


# A line of whitespace alone, longer than the size: it gave no pieces, and the
# lines held back to open the first piece were dropped (Codex's diff review).
DECISION = "Ari decided to take the train."
BLANK = " " * 1600
WHITESPACE_CASES = {
    "between two lines": f"{DECISION}\n{BLANK}\nSam agreed.",
    "after a heading": f"## Plans\n\n{DECISION}\n{BLANK}\nSam agreed.\n",
    "right after a heading": f"## Plans\n{BLANK}\n{DECISION}\nSam agreed.\n",
    "first in the file": f"{BLANK}\n{DECISION}\nSam agreed.",
    "last in the file": f"{DECISION}\nSam agreed.\n" + "\t" * 1600,
    "of mixed whitespace": f"{DECISION}\n" + "　 \t " * 500 + "\nSam agreed.",
    "between long lines": f"{DECISION}\n{_long_line(2000)}\n{BLANK}\n{_long_line(2000, 2)}",
    "after a long section": "## Plans\n\n" + "\n".join(
        f"Line {n} of the plan Ari and Sam wrote." for n in range(60))
        + f"\n{DECISION}\n{BLANK}\nSam agreed.\n",
}


def _words(text: str) -> set[str]:
    return set(text.split())


@pytest.mark.parametrize("text", WHITESPACE_CASES.values(), ids=WHITESPACE_CASES.keys())
def test_a_long_line_of_whitespace_loses_nothing_around_it(text: str) -> None:
    chunks = chunk_markdown(text, source="notes/plans.md")
    assert _words(text) <= set().union(*(_words(c.content) for c in chunks))
    assert max(len(c.content) for c in chunks) <= max_chunk_chars()


def test_a_decision_beside_a_long_line_of_whitespace_is_indexed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "tree"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "plans.md").write_text(WHITESPACE_CASES["between two lines"],
                                             encoding="utf-8")
    monkeypatch.setattr(embedding, "embedder", lambda: StubEmbedding())
    db = tmp_path / "index.db"
    indexer.index_tree(root=root, db=db)
    conn = sqlite3.connect(db)
    try:
        found = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH "
                             "'decided train'").fetchone()[0]
    finally:
        conn.close()
    assert found == 1


_SPACES = " \t　 "


def _hostile_document(rng: random.Random, size: int) -> str:
    """Sections with every kind of line beside every other: blank ones, lines
    of whitespace alone (some far longer than the size), short and long text,
    "words" longer than the size, long headings, long lines first and last.
    Each section has text; every word is unique (``a<n>z``, or ``a<n>q...qz``)."""
    counter = 0

    def text_line(target: int, token: bool = False) -> str:
        nonlocal counter
        if token:  # one "word" of about target characters, with no space to cut at
            counter += 1
            return f"a{counter}" + "q" * target + "z"
        words: list[str] = []
        length = 0
        while length < target:
            counter += 1
            words.append(f"a{counter}z")
            length += len(words[-1]) + 1
        return " ".join(words)

    def any_line() -> str:
        kind = rng.randrange(7)
        if kind == 0:
            return ""
        if kind == 1:
            return "".join(rng.choice(_SPACES) for _ in range(rng.randint(1, 3 * size)))
        if kind == 2:
            return text_line(rng.randint(1, size // 2))
        if kind == 3:
            return text_line(rng.randint(size // 2, size))
        if kind == 4:
            return text_line(rng.randint(size + 1, 4 * size))
        if kind == 5:
            return text_line(rng.randint(size, 3 * size), token=True)
        return rng.choice(["  ", "\t", "- " + text_line(rng.randint(1, size))])

    lines: list[str] = []
    for s in range(rng.randint(1, 4)):
        heading = text_line(rng.randint(size, 2 * size)) if rng.random() < 0.15 \
            else f"Section {s}"
        lines.append("#" * rng.randint(1, 3) + " " + heading)
        body = [any_line() for _ in range(rng.randint(0, 14))]
        body.insert(rng.randint(0, len(body)), text_line(rng.randint(1, 2 * size)))
        lines += body
    return "\n".join(lines) + rng.choice(["", "\n", "\n" + " " * (2 * size)])


@pytest.mark.parametrize("size", [60, 300, MAX_CHUNK_SIZE])
@pytest.mark.parametrize("overlap", [0, OVERLAP_LINES])
def test_no_text_is_lost_whatever_the_lines_hold(size: int, overlap: int) -> None:
    rng = random.Random(size * 7 + overlap)
    bound = max_chunk_chars(size, overlap)
    for _ in range(60):
        text = _hostile_document(rng, size)
        chunks = chunk_markdown(text, max_chunk_size=size, overlap_lines=overlap)
        assert all(len(c.content) <= bound for c in chunks)
        # Every word of the input is in the chunks, in one piece once the
        # whitespace between chunks is taken away (a cut can split a word
        # with no space in it, or, as upstream does, an overlap's word).
        joined = re.sub(r"\s", "", "".join(c.content for c in chunks))
        assert set(re.findall(r"a\d+q*z", text)) <= set(re.findall(r"a\d+q*z", joined))


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
