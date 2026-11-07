"""Comments and docstrings state the current design, not the project's history (H13.10).

Review numbers, outside and Codex reviews, rechecks and hardening item IDs
belong in commit messages and ``.ai/features/hardening.md``: in code they go
stale and make the code slower to learn. The scan covers ``src/``,
``narada/`` and the reference profile's source: every comment (tokenize),
every docstring (ast), and the comment lines of shell scripts. Tests may
cite reviews and items.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCANNED = [REPO / "src", REPO / "narada", REPO / "profiles" / "reference" / "src"]
SKIPPED = {"_vendored", "__pycache__"}  # vendored code keeps its own comments

PATTERNS = {
    "a review number": re.compile(r"\breview\s*#\s*\d+", re.IGNORECASE),
    "an outside review": re.compile(r"\boutside[- ]review\b", re.IGNORECASE),
    "a Codex review": re.compile(r"\bcodex\s+review\b", re.IGNORECASE),
    "a recheck": re.compile(r"\brecheck\b", re.IGNORECASE),
    "a hardening item": re.compile(r"\bH\d+(?:\.\d+)?\b"),
}

# (path relative to the repo, the matched text) -> why it may stay. Nothing needs one.
ALLOWED: dict[tuple[str, str], str] = {}


def _python_text(source: str) -> Iterator[tuple[int, str]]:
    """Every comment and docstring line of *source*: (line number, text)."""
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            yield tok.start[0], tok.string
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                first = node.body[0].lineno
                for offset, line in enumerate(doc.splitlines()):
                    yield first + offset, line


def _shell_text(source: str) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(source.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            yield number, line


def _hits(source: str, *, shell: bool = False) -> Iterator[tuple[int, str, str]]:
    """(line, what, matched text) for each history reference in *source*'s comments."""
    for number, text in (_shell_text(source) if shell else _python_text(source)):
        for what, pattern in PATTERNS.items():
            for m in pattern.finditer(text):
                yield number, what, m.group(0)


def _files() -> Iterator[Path]:
    for base in SCANNED:
        for path in sorted(base.rglob("*")):
            if SKIPPED & set(path.parts) or not path.is_file():
                continue
            if path.suffix in (".py", ".sh"):
                yield path


def test_comments_and_docstrings_cite_no_review_or_hardening_item() -> None:
    found = []
    for path in _files():
        rel = path.relative_to(REPO).as_posix()
        source = path.read_text(encoding="utf-8")
        for number, what, text in _hits(source, shell=path.suffix == ".sh"):
            if (rel, text) not in ALLOWED:
                found.append(f"{rel}:{number}: {what} ({text!r})")
    assert not found, ("state the rule the code keeps; the history goes in the commit "
                       "message\n  " + "\n  ".join(found))


def test_the_scan_finds_what_it_looks_for_and_nothing_else() -> None:
    source = '''"""Module. Kept since outside review #5 (H12.3)."""
ZONE_RECHECK_S = 1.0  # a constant's name: no match
# The ``##`` heading, a fixed-width look-back.
def f() -> str:
    """Fixed after the Codex review; recheck clean."""
    return "H12.1 in a string is code, not a comment"
'''
    found = sorted((number, what) for number, what, _text in _hits(source))
    assert found == [(1, "a hardening item"), (1, "a review number"), (1, "an outside review"),
                     (5, "a Codex review"), (5, "a recheck")]
    assert [what for _n, what, _t in _hits("# from H13\necho H13\n", shell=True)] == [
        "a hardening item"]
