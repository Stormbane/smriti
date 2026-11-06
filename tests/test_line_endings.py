"""H12.16: an append to a daily file keeps the file's line ending.

H1.4 made the append binary, so on Windows a day's new entries were LF
beside the CRLF ones written before it (text mode). An append now takes
the ending of the file's first line; a new file gets the platform's, as a
text-mode create gave. Each test writes the file's ending itself, so both
run on every platform.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from smriti.core.tree import tree_root
from smriti.store.writer import write_entry


def _entry(text: str) -> Path:
    return write_entry(text, branch="notes", title="t", root=tree_root(), reindex=False)


def test_a_new_daily_file_takes_the_platform_line_ending() -> None:
    data = _entry("one\ntwo").read_bytes()
    assert data.count(b"\n") == data.count(os.linesep.encode())


@pytest.mark.parametrize("ending", [b"\r\n", b"\n"], ids=["crlf", "lf"])
def test_an_append_keeps_the_files_line_ending(ending: bytes) -> None:
    path = _entry("first entry")
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", ending))
    assert _entry("second\nentry\r\nwith a CRLF of its own") == path
    data = path.read_bytes()
    assert all(line.endswith(b"\r") == (ending == b"\r\n")
               for line in data.split(b"\n")[:-1]), data
    assert b"\r\r" not in data
    text = path.read_text(encoding="utf-8")
    assert text.count("\n---\n\n---\n") == 1 and "second\nentry\nwith a CRLF" in text
