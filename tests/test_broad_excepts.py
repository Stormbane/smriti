"""H12.7: every broad except says why it is broad.

The rule (CLAUDE.md, ``.ai/knowledge/conventions.md``): narrow an ``except
Exception`` to what can be raised; keep one only where any failure must be
survived (a top-level handler, a hook, a non-critical step), or where every
failure is cleaned up after, recorded and raised again, with ``# noqa: BLE001
— <reason>`` on the line. Ruff's BLE001 can't hold it: it accepts a broad
handler that logs or re-raises, so such handlers went without a reason.

This scans the source (AST) for handlers that catch ``Exception`` or
``BaseException`` (alone or in a tuple) or everything (a bare ``except:``),
and fails on one whose ``except`` line has no reason after the code.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCANNED = [REPO / "src", REPO / "profiles" / "reference" / "src", REPO / "narada",
           REPO / "scripts"]
VENDORED = REPO / "src" / "smriti" / "_vendored"  # third-party code, kept close to upstream

# Being restructured on another branch (hardening/h12s), which annotates their
# excepts itself; the scan covers them once both branches are merged.
NOT_YET: tuple[str, ...] = (
    "src/smriti/store/indexer.py",
    "src/smriti/store/search.py",
    "src/smriti/embed/",
)

BROAD = frozenset({"Exception", "BaseException"})
# The code, a dash of any kind, then a reason: at least one word.
REASON = re.compile(r"#\s*noqa:\s*BLE001\s*[—–-]+\s*\w")


def _caught(node: ast.expr | None) -> set[str]:
    """The names a handler's type catches; "" for a bare ``except:``."""
    if node is None:
        return {""}
    if isinstance(node, ast.Tuple):
        return set().union(*(_caught(e) for e in node.elts))
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):  # builtins.Exception
        return {node.attr}
    return set()


def unexplained(source: str) -> list[int]:
    """The lines of broad handlers in ``source`` whose ``except`` line gives no reason.

    The reason may sit on any line of the clause up to its colon, as ruff
    reads a ``noqa`` on a line its finding spans.
    """
    lines = source.splitlines()
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _caught(node.type) & (BROAD | {""}):
            continue
        last = node.type.end_lineno if node.type is not None else node.lineno
        clause = lines[node.lineno - 1:(last or node.lineno)]
        if not any(REASON.search(line) for line in clause):
            bad.append(node.lineno)
    return bad


def _sources() -> Iterator[Path]:
    for top in SCANNED:
        for path in sorted(top.rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if path.is_relative_to(VENDORED) or rel.startswith(NOT_YET):
                continue
            yield path


def test_every_broad_except_gives_its_reason() -> None:
    found = {}
    for path in _sources():
        bad = unexplained(path.read_text(encoding="utf-8"))
        if bad:
            found[path.relative_to(REPO).as_posix()] = bad
    assert not found, (
        "a broad except without `# noqa: BLE001 — <reason>`: narrow it to what can be "
        f"raised, or say why any failure must be caught: {found}")


def test_the_scan_reads_the_tree() -> None:
    """It finds the source, and broad handlers in it, so an empty pass means something."""
    paths = list(_sources())
    assert any(p.as_posix().endswith("smriti/mcp/server.py") for p in paths)
    assert any(p.parts[-3:] == ("narada", ".smriti", "wake.py") for p in paths)
    handlers = sum(
        1 for p in paths for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
        if isinstance(n, ast.ExceptHandler) and _caught(n.type) & BROAD)
    assert handlers > 40


@pytest.mark.parametrize(("clause", "flagged"), [
    ("except Exception:", True),
    ("except BaseException:", True),
    ("except:", True),
    ("except (ValueError, Exception) as exc:", True),
    ("except builtins.Exception:", True),
    ("except Exception:  # noqa: BLE001", True),  # the code without a reason
    ("except Exception:  # noqa: BLE001 —", True),
    ("except Exception:  # an aside, but no noqa", True),
    ("except Exception:  # noqa: BLE001 — a hook never fails its caller", False),
    ("except Exception:  # noqa: BLE001 - logged; the loop goes on", False),
    ("except Exception as e:  # noqa: BLE001 — audited  # pragma: no cover", False),
    ("except (ValueError, OSError):", False),
    ("except ValueError:", False),
])
def test_what_the_scan_flags(clause: str, flagged: bool) -> None:
    source = f"try:\n    pass\n{clause}\n    raise\n"
    assert unexplained(source) == ([3] if flagged else [])


def test_a_reason_on_a_later_line_of_the_clause_counts() -> None:
    source = ("try:\n    pass\nexcept (\n    ValueError,\n"
              "    Exception,  # noqa: BLE001 — a hook never fails its caller\n):\n    pass\n")
    assert unexplained(source) == []
