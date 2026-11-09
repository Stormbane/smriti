"""Safe edits to files in the memory tree.

Several processes write the tree at once: an MCP server per open session,
the nightly, the day-log watcher. An edit that reads a file, changes it and
writes it back loses any edit that lands in between, so it holds the file's
:func:`edit_lock` from the read to the write. Rewrites go through
:func:`write_atomic`, so a reader sees the old file or the new one, never
half of either; new files that must not overwrite go through
:func:`create_new`.

Atomic isn't durable: a write the OS still holds in memory is lost with the
machine (a power cut), and the file can come back empty. A write that must
outlast that says ``durable=True``: the file's bytes reach the disk
(``fsync``) before it replaces the old one, and on POSIX its directory's
entry after, best effort (:func:`sync_dir`). It costs a flush per write
(about 0.75 ms on an SSD, with rare longer stalls), so it is for memory
text, which nothing can rebuild, not for the index, caches or state a
later run recomputes.
"""

from __future__ import annotations

import itertools
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

from smriti.core.oslock import BUSY, UNAVAILABLE, lock_path, try_lock_why


class FileBusy(TimeoutError):
    """Another writer held the file for longer than we would wait."""


class LockUnavailable(OSError):
    """No edit lock could be opened at all (its directory can't be used), so
    the edit would exclude no other writer; waiting won't change that."""


@contextmanager
def edit_lock(path: Path, wait_s: float = 10.0) -> Iterator[None]:
    """Hold the edit lock for ``path``, or raise :class:`FileBusy` (another
    writer holds it) or :class:`LockUnavailable` (no lock can be had).

    The lock is an OS lock keyed by the resolved path, so it is released if
    its holder crashes, and junctioned paths to one file share it.
    """
    key = lock_path("edit", os.path.normcase(str(Path(path).resolve())))
    with try_lock_why(key, wait_s=wait_s) as (outcome, why):
        if outcome == BUSY:
            raise FileBusy(f"{path} is being edited elsewhere; try again")
        if outcome == UNAVAILABLE:
            raise LockUnavailable(
                f"{path} was not changed: its edit lock is unavailable ({why}). Check the "
                "runtime directory (SMRITI_RUNTIME_DIR)")
        yield


def sync_file(f: IO[Any]) -> None:
    """Flush the open file *f* to the disk: Python's buffer, then the OS's (``fsync``)."""
    f.flush()
    os.fsync(f.fileno())


def sync_dir(directory: Path) -> None:
    """Flush *directory*'s entries to the disk, so a new or renamed name in it lasts. Best effort.

    On POSIX a rename or a new file is durable once its directory is, so
    the directory is fsynced; a file system that refuses that is let be.
    Windows has no handle to flush a directory by (NTFS journals the
    rename), so there this does nothing.
    """
    if sys.platform == "win32":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # EINVAL on some file systems: the rename stands as it is
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str | bytes, *, mode: int | None = None,
                 durable: bool = False) -> None:
    """Replace ``path`` with ``text`` in one step, or raise :class:`FileBusy`.

    Text is written in text mode (the platform's line endings), bytes as they are.
    The temp file's name is this process's and thread's, so writers never share one.
    ``mode``, when given, is the new file's permission bits, set before any of
    ``text`` is written (a file only its owner may read stays so). ``durable``
    fsyncs the temp file before it replaces ``path``, and on POSIX the
    directory after (see the module docstring).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        if mode is not None:
            os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
            os.chmod(tmp, mode)
        if isinstance(text, bytes):
            with open(tmp, "wb") as fb:
                fb.write(text)
                if durable:
                    sync_file(fb)
        else:
            with open(tmp, "w", encoding="utf-8") as ft:
                ft.write(text)
                if durable:
                    sync_file(ft)
        for attempt in range(40):
            try:
                os.replace(tmp, path)
            except PermissionError:
                # Windows refuses while a reader has the file open; it closes soon.
                time.sleep(0.01 * (attempt + 1))
                continue
            if durable:
                sync_dir(path.parent)
            return
        raise FileBusy(f"{path} stayed open elsewhere; it was not changed")
    finally:
        tmp.unlink(missing_ok=True)


def create_new(path: Path, text: str, *, durable: bool = False) -> Path:
    """Create ``path``, or ``<stem>-2``, ``<stem>-3``... if it exists. Never overwrites.

    Returns the path written. ``durable`` fsyncs the file, and on POSIX its
    directory, before returning.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for n in itertools.count(1):
        candidate = path if n == 1 else path.with_name(f"{path.stem}-{n}{path.suffix}")
        try:
            with candidate.open("x", encoding="utf-8") as f:
                f.write(text)
                if durable:
                    sync_file(f)
        except FileExistsError:
            continue
        if durable:
            sync_dir(candidate.parent)
        return candidate
    raise AssertionError("unreachable")
