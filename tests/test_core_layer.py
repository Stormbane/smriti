"""``smriti.core`` depends on nothing above it (H13.6).

``core`` is the bottom layer: files and locks, the tree's root, the
runtime directory, the scrub and the day zone. Every other package builds
on it (``store``, ``daylog``, ``embed``, ``mcp``, ``wake``, ``install``,
``integrations`` and the rest), so an import the other way is a cycle in
waiting and drags a heavier package into everything that needs only core:
outside review #6 found ``core.memwrite`` importing ``daylog.scrub`` and
``core.oslock`` importing ``embed.runtime``.

This scans every module under ``src/smriti/core`` (AST, so an import
inside a function counts as much as one at the top) and fails on an import
of any smriti package but ``core``: ``import smriti.x``, ``from smriti.x
import ...``, ``from smriti import x``, a relative import that climbs out of
``core``, and ``importlib.import_module`` (or ``__import__``) with a
literal name.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "src" / "smriti" / "core"
# The packages review #6 named; any other smriti package fails the same way.
ABOVE = ("smriti.store", "smriti.daylog", "smriti.embed", "smriti.mcp", "smriti.wake",
         "smriti.install", "smriti.integrations")


def _outside_core(name: str) -> bool:
    """A smriti module that isn't core (``smriti`` itself, the package root, is neither)."""
    parts = name.split(".")
    return parts[0] == "smriti" and len(parts) > 1 and parts[1] != "core"


def _resolve(module: str, level: int, here: str) -> str:
    """The absolute name a relative import in module *here* names."""
    base = here.split(".")[:-level]  # a module's package is its name less the last part
    return ".".join(base + ([module] if module else []))


def violations(source: str, here: str, where: str = "<source>") -> Iterator[str]:
    """Each import in *source* (the module *here*) of a smriti package other than core."""
    for node in ast.walk(ast.parse(source)):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = _resolve(module, node.level, here)
            # `from smriti import daylog` names the package in the imported names.
            targets = [f"{module}.{a.name}" for a in node.names] if module == "smriti" else [
                module]
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    targets = [arg.value]
        for target in targets:
            if _outside_core(target):
                yield f"{where}:{node.lineno}: {target}"


def _core_modules() -> Iterator[tuple[str, Path]]:
    for path in sorted(CORE.rglob("*.py")):
        parts = ["smriti", "core", *path.relative_to(CORE).with_suffix("").parts]
        if parts[-1] == "__init__":
            parts.append("__init__")  # a package's relative imports start from the package
        yield ".".join(parts), path


def test_core_imports_nothing_above_it() -> None:
    found = [v for module, path in _core_modules()
             for v in violations(path.read_text(encoding="utf-8"), module,
                                 path.relative_to(REPO).as_posix())]
    assert not found, "smriti.core imports from a package above it:\n" + "\n".join(found)


def test_the_scan_sees_every_form_of_import() -> None:
    """Each form the scan must catch, on a module that has it."""
    here = "smriti.core.example"
    for package in ABOVE:
        tail = package.split(".")[1]
        for source in (
                f"import {package}.thing\n",
                f"from {package}.thing import name\n",
                f"from smriti import {tail}\n",
                f"from ..{tail} import thing\n",
                f"from .. import {tail}\n",
                f"def later():\n    from {package} import thing\n",
                f"class C:\n    def m(self):\n        import {package}\n",
                f"import importlib\nimportlib.import_module('{package}.thing')\n",
                f"__import__('{package}')\n",
        ):
            assert list(violations(source, here)), source
    for fine in ("import os\n", "from smriti.core.files import write_atomic\n",
                 "from . import files\n", "from .rundir import runtime_dir\n",
                 "import smriti\n", "from smriti import core\n",
                 "def f():\n    from smriti.core.tree import tree_root\n"):
        assert not list(violations(fine, here)), fine


def test_the_moved_names_are_the_same_objects_where_they_were() -> None:
    """The scrub and the day zone moved into core; their old homes re-export them."""
    from smriti.core import dayzone
    from smriti.core.scrub import scrub
    from smriti.daylog import model
    from smriti.daylog.scrub import scrub as old_scrub

    assert old_scrub is scrub
    assert model.day_tz is dayzone.day_tz and model.SYSTEM_TIME is dayzone.SYSTEM_TIME
    assert model.ZONE_RECHECK_S == dayzone.ZONE_RECHECK_S
