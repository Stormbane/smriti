"""Multi-path file scanner for markdown knowledge bases.

---
Vendored from zilliztech/memsearch (MIT License, (c) 2025 Zilliz Inc.)
Upstream: https://github.com/zilliztech/memsearch/blob/main/src/memsearch/scanner.py
Vendored on: 2026-04-12
Modifications from upstream:
  - scan_paths: added ignore_dirs parameter to prune well-known build/vendor
    directories (node_modules, venv, etc.) during os.walk. Pruning at walk
    time matters for large repos where post-walk filtering is too late.
License: see smriti/NOTICE.md and smriti/src/smriti/_vendored/memsearch/LICENSE
---
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScannedFile:
    """Metadata for a discovered markdown file."""

    path: Path
    mtime: float
    size: int


DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset({
    "node_modules",
    "__pycache__",
    "venv",
    ".venv",
    "site-packages",
    "dist",
    "build",
    "target",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
})


def scan_paths(
    paths: list[str | Path],
    *,
    extensions: tuple[str, ...] = (".md", ".markdown"),
    ignore_hidden: bool = True,
    ignore_dirs: frozenset[str] | set[str] | None = None,
) -> list[ScannedFile]:
    """Recursively scan *paths* for markdown files.

    Each entry in *paths* may be a file or directory.  Directories are
    walked recursively.  Hidden files/dirs (starting with ``"."``) are
    skipped when *ignore_hidden* is ``True``.  Directory names in
    *ignore_dirs* are pruned during the walk; defaults to
    ``DEFAULT_IGNORE_DIRS`` (node_modules, venv, build, etc.).
    """
    results: list[ScannedFile] = []
    seen: set[str] = set()
    prune = DEFAULT_IGNORE_DIRS if ignore_dirs is None else frozenset(ignore_dirs)

    for p in paths:
        root = Path(p).expanduser().resolve()
        if root.is_file():
            _maybe_add(root, extensions, seen, results)
        elif root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames
                    if not (ignore_hidden and d.startswith("."))
                    and d not in prune
                ]
                for fname in filenames:
                    if ignore_hidden and fname.startswith("."):
                        continue
                    fp = Path(dirpath) / fname
                    _maybe_add(fp, extensions, seen, results)

    results.sort(key=lambda f: f.path)
    return results


def _maybe_add(
    fp: Path,
    extensions: tuple[str, ...],
    seen: set[str],
    results: list[ScannedFile],
) -> None:
    if fp.suffix.lower() not in extensions:
        return
    real = str(fp.resolve())
    if real in seen:
        return
    seen.add(real)
    stat = fp.stat()
    results.append(ScannedFile(path=fp, mtime=stat.st_mtime, size=stat.st_size))
