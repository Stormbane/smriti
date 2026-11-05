"""Safe edits to files in the memory tree.

Several processes write the tree at once: an MCP server per open session,
the nightly, the day-log watcher. An edit that reads a file, changes it and
writes it back loses any edit that lands in between, so it holds the file's
:func:`edit_lock` from the read to the write. Rewrites go through
:func:`write_atomic`, so a reader sees the old file or the new one, never
half of either; new files that must not overwrite go through
:func:`create_new`.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from smriti.core.oslock import exclusive_lock, lock_path


class FileBusy(TimeoutError):
    """Another writer held the file for longer than we would wait."""


@contextmanager
def edit_lock(path: Path, wait_s: float = 10.0) -> Iterator[None]:
    """Hold the edit lock for ``path``, or raise :class:`FileBusy`.

    The lock is an OS lock keyed by the resolved path, so it is released if
    its holder crashes, and junctioned paths to one file share it.
    """
    key = lock_path("edit", os.path.normcase(str(Path(path).resolve())))
    with exclusive_lock(key, wait_s=wait_s) as held:
        if not held:
            raise FileBusy(f"{path} is being edited elsewhere; try again")
        yield


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, or raise :class:`FileBusy`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        for attempt in range(40):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows refuses while a reader has the file open; it closes soon.
                time.sleep(0.01 * (attempt + 1))
        raise FileBusy(f"{path} stayed open elsewhere; it was not changed")
    finally:
        tmp.unlink(missing_ok=True)


def create_new(path: Path, text: str) -> Path:
    """Create ``path``, or ``<stem>-2``, ``<stem>-3``... if it exists. Never overwrites.

    Returns the path written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for n in itertools.count(1):
        candidate = path if n == 1 else path.with_name(f"{path.stem}-{n}{path.suffix}")
        try:
            with candidate.open("x", encoding="utf-8") as f:
                f.write(text)
        except FileExistsError:
            continue
        return candidate
    raise AssertionError("unreachable")
