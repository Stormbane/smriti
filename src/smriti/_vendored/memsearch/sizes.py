"""The chunker's sizes: its defaults, and the longest chunk it makes with them.

smriti's own module, not upstream's: chunker.py reads its defaults here, and
readers that size what they show (smriti_read) import this alone, which
costs nothing, where the chunker's imports (hashlib among them) cost an MCP
server's start several milliseconds.
"""

MAX_CHUNK_SIZE = 1500
OVERLAP_LINES = 2


def max_chunk_chars(
    max_chunk_size: int = MAX_CHUNK_SIZE,
    overlap_lines: int = OVERLAP_LINES,
) -> int:
    """The longest chunk :func:`chunker.chunk_markdown` makes with these settings.

    A chunk is at most *max_chunk_size* characters, with one exception: a
    chunk that starts with the lines it repeats from the chunk before it
    (at most *overlap_lines* of them) and holds the one line that didn't fit
    beside them. That chunk is at most *overlap_lines* + 1 lines, each at
    most *max_chunk_size* characters (a longer line is cut into pieces),
    joined by newlines. With *overlap_lines* 0 no chunk exceeds the size.
    """
    return (overlap_lines + 1) * max_chunk_size + overlap_lines
