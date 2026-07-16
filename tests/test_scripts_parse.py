"""Every script in scripts/ must at least compile.

The 2026-07-17 diff review caught scripts/install.py shipping with a
syntax error — nothing imported it, so the test suite was green while
every install mode was broken. Cheap guard: compile them all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPTS = sorted(SCRIPTS_DIR.glob("*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script: Path) -> None:
    ast.parse(script.read_text(encoding="utf-8"), filename=str(script))


def test_scripts_found() -> None:
    assert SCRIPTS, f"no scripts found under {SCRIPTS_DIR}"
