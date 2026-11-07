"""The one way memory text gets into the tree: masked, then written safely.

Everything smriti writes into the memory tree as text comes through here:
entries, findings and their cross-project stubs, decisions, feature specs
and their status changes, todos, the handoff, the reference profile's
outbox, rendered day logs, digests, rollups and ``index.md`` listings. The
tree is committed and pushed, and an agent writes down whatever it was
shown (a model can be made to copy a key into a digest, too), so credential
shapes are masked first (:func:`smriti.core.scrub.scrub`) in the whole
text: the body, and the titles and metadata that land in the file with it.
A name that goes into a path is masked by its caller with :func:`mask`.
The write itself goes through :mod:`smriti.core.files`.

Every write here is durable by default (``durable=True``): the text is on
the disk before the call returns, so a power cut can't empty a journal, a
todo list or the handoff. A caller whose file a later write rebuilds from
the tree (an ``index.md`` listing) passes ``durable=False``.

``tests/test_memwrite.py`` scans ``src/`` and fails on a file write
anywhere else, outside an allowlist of the writers of runtime and index
state.
"""

from __future__ import annotations

import os
from pathlib import Path

from smriti.core.files import create_new, edit_lock, sync_dir, sync_file, write_atomic
from smriti.core.scrub import scrub

# How much of a file is read to learn its line ending: its first line ends
# well inside this (frontmatter, or a heading).
_HEAD_BYTES = 4096


def mask(text: str) -> str:
    """*text* with its credential shapes masked."""
    return scrub(text)


def write_memory(path: Path, text: str, *, durable: bool = True) -> str:
    """Replace *path* with *text*, masked, in one step (:func:`write_atomic`).

    Returns the masked text, which is what a reader of *path* gets (with
    ``read_text``'s newlines). A read-modify-write holds the file's
    :func:`edit_lock` from its read to this.
    """
    masked = scrub(text)
    write_atomic(path, masked, durable=durable)
    return masked


def create_memory(path: Path, text: str, *, unique: bool = True,
                  durable: bool = True) -> Path:
    """Create *path* holding *text*, masked; never overwrites. Returns the path written.

    With *unique*, a name that exists becomes ``<stem>-2``, ``<stem>-3``...
    (:func:`create_new`); without it, an existing *path* raises
    :class:`FileExistsError`.
    """
    masked = scrub(text)
    if unique:
        return create_new(path, masked, durable=durable)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        f.write(masked)
        if durable:
            sync_file(f)
    if durable:
        sync_dir(path.parent)
    return path


def _line_ending(head: bytes) -> bytes:
    """The line ending of a file that starts with *head*: its first line's, else the platform's."""
    end = head.find(b"\n")
    if end < 0:
        return os.linesep.encode("ascii")
    return b"\r\n" if head[end - 1:end] == b"\r" else b"\n"


def append_memory(path: Path, text: str, *, header: str = "", separator: str = "",
                  durable: bool = True) -> None:
    """Append *text*, masked, to *path* under its edit lock.

    A missing or empty file gets *header* first; a file with content gets
    *separator* first. What is appended takes the file's line ending, read
    from its first line, so a CRLF file stays CRLF and an LF file LF; a new
    file gets the platform's, as a text-mode write gives. Every session's MCP
    server can append to the same day's file, and Windows appends aren't
    atomic between processes, so the lock covers the read and the write.
    *durable* fsyncs the file before the lock is let go, and on POSIX its
    directory when the file was new.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with edit_lock(path), path.open("a+b") as f:
        f.seek(0)
        head = f.read(_HEAD_BYTES)
        block = scrub(((separator if head else header) + text).replace("\r\n", "\n"))
        newline = _line_ending(head)
        if newline != b"\n":
            block = block.replace("\n", newline.decode("ascii"))
        f.seek(0, os.SEEK_END)
        f.write(block.encode("utf-8"))
        if durable:
            sync_file(f)
            if not head:  # a new file: its name must last too
                sync_dir(path.parent)
