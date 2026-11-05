"""Values written into a markdown file's frontmatter or heading lines.

A frontmatter key, a heading, or the closing ``---`` can only start on a
new line, so a value that can't contain one can't forge any of them.
:func:`one_line` folds every line break and other whitespace run (including
``\\r``, tabs and the Unicode line separators) into one space.
"""

from __future__ import annotations


def one_line(value: object) -> str:
    """``value`` as one line: whitespace runs, line breaks included, become one space."""
    return " ".join(str(value).split())
