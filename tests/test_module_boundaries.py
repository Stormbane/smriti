"""H12.5: no module uses another package's private names.

Outside review #5 (Opus problem 4) found search importing the indexer's
private embedding helpers, the embedding service importing three more, and
the CLI the morning's. Each now has a public home (the embedding API is
``smriti.embed.provider``). This test scans the source (AST) and fails on a
name starting with ``_`` taken from a module of another top-level package:
``from smriti.<a>... import _name``, ``smriti.<a>..._name``, or ``m._name``
where ``m`` is bound to such a module. ``smriti.store`` and ``smriti.embed``
are different packages; ``smriti.cli`` is a package of its own. A module
whose name starts with ``_`` is private too, except ``smriti._vendored``,
which holds third-party code used through its public names. Tests are exempt.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCANNED = [REPO / "src", REPO / "profiles" / "reference" / "src"]
VENDORED = "smriti._vendored"


def _modules() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for base in SCANNED:
        for path in base.rglob("*.py"):
            parts = list(path.relative_to(base).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            found[".".join(parts)] = path
    return found


MODULES = _modules()


def _package(module: str) -> str:
    """``smriti.store.search`` -> ``smriti.store``; ``smriti.cli`` -> ``smriti.cli``."""
    parts = module.split(".")
    return ".".join(parts[:2]) if parts[0] == "smriti" else parts[0]


def _private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _private_module(module: str) -> bool:
    """A module named with a ``_`` below its top-level package (``_vendored`` is public here)."""
    parts = module.split(".")
    inner = parts[2:] if parts[0] == "smriti" else parts[1:]
    return any(_private(p) for p in inner)


def _violations(module: str, path: Path) -> Iterator[str]:
    mine = _package(module)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else path.name
    aliases: dict[str, str] = {}  # a local name -> the smriti module it is bound to

    def foreign(target: str) -> bool:
        return target.split(".")[0].startswith("smriti") and _package(target) != mine

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if foreign(a.name) and _private_module(a.name):
                    yield f"{where}:{node.lineno}: import {a.name}"
                if a.asname:
                    aliases[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if not foreign(node.module):
                continue
            if _private_module(node.module) and not node.module.startswith(VENDORED + "."):
                yield f"{where}:{node.lineno}: from {node.module} import ..."
            for a in node.names:
                if _private(a.name):
                    yield f"{where}:{node.lineno}: from {node.module} import {a.name}"
                elif f"{node.module}.{a.name}" in MODULES:
                    aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and _private(node.attr)):
            continue
        chain: list[str] = []
        base: ast.expr = node.value
        while isinstance(base, ast.Attribute):
            chain.append(base.attr)
            base = base.value
        if not isinstance(base, ast.Name):
            continue
        root = aliases.get(base.id, base.id if base.id.startswith("smriti") else "")
        target = ".".join([root, *reversed(chain)]) if root else ""
        if target and target in MODULES and foreign(target):
            yield f"{where}:{node.lineno}: {target}.{node.attr}"


def test_no_module_imports_another_packages_private_names() -> None:
    found = [v for module, path in sorted(MODULES.items())
             if not module.startswith(VENDORED)
             for v in _violations(module, path)]
    assert not found, "private names used across packages:\n" + "\n".join(found)


def test_the_scan_sees_each_kind_of_private_use(tmp_path: Path) -> None:
    """The scan itself: each form is caught across packages, and none within one."""
    source = (
        "import smriti.store.indexer as ix\n"
        "from smriti.embed import provider as emb\n"
        "from smriti.store.indexer import _embed\n"
        "from smriti.daylog.lock import writer_lock\n"
        "from smriti.store.search import fts_terms\n"
        "import smriti.store.search\n"
        "ix._embed\n"
        "emb._loaded\n"
        "smriti.store.search._STOPWORDS\n"
        "emb.embedder\n"
    )
    path = tmp_path / "probe.py"
    path.write_text(source, encoding="utf-8")
    assert [v.split(": ", 1)[1] for v in _violations("smriti.cli", path)] == [
        "from smriti.store.indexer import _embed",
        "smriti.store.indexer._embed",
        "smriti.embed.provider._loaded",
        "smriti.store.search._STOPWORDS",
    ]
    assert list(_violations("smriti.store.probe", path)) == [
        "probe.py:8: smriti.embed.provider._loaded"]
