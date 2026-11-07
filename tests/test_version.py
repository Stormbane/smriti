"""H12.5 and H12.4: one version string, and tool texts that match their code.

Outside review #5: ``serverInfo.version`` said 0.1.0 against package 0.2.0,
and ``smriti_add_todo`` said it "substring-dedups" though it matches whole
phrases. Since H12.4 the build reads the version from ``smriti.__version__``
(pyproject declares it dynamic), so the two can't drift; the CLI and the
installer report it too.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import smriti
from smriti.mcp.server import handle_message
from smriti.mcp.tools import TOOLS

REPO = Path(__file__).resolve().parents[1]


def test_the_server_reports_the_package_version() -> None:
    reply = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert reply is not None
    assert reply["result"]["serverInfo"] == {"name": "smriti", "version": smriti.__version__}


def test_the_build_reads_the_version_from_the_package() -> None:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" not in data["project"]
    assert "version" in data["project"]["dynamic"]
    assert data["tool"]["hatch"]["version"]["path"] == "src/smriti/__init__.py"
    source = (REPO / "src" / "smriti" / "__init__.py").read_text(encoding="utf-8")
    assert re.findall(r'^__version__ = "([^"]+)"$', source, re.MULTILINE) == [smriti.__version__]


@pytest.mark.parametrize("module", ["smriti.cli", "smriti.install.cli"])
def test_the_cli_and_the_installer_report_it(
        module: str, capsys: pytest.CaptureFixture[str]) -> None:
    import importlib

    main = importlib.import_module(module).main
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"smriti {smriti.__version__}"


def test_add_todo_says_how_it_dedupes() -> None:
    from smriti.mcp.tools.projects import _mentions

    tool = next(t for t in TOOLS if t.name == "smriti_add_todo")
    assert "whole phrase" in tool.description
    assert not re.search(r"substring", tool.description, re.IGNORECASE)
    assert _mentions("- [ ] Fix the lamp wick", "fix the  LAMP")
    assert not _mentions("- [ ] Add a prefix to names", "fix")
