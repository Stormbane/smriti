"""Config lint tests — verify no active instructions reference deprecated paths.

These tests scan the live Claude Code configuration for references to the
old memory system (.ai/memory/coder/, ~/.claude/projects/*/memory/) to
ensure the agent is not being told to write to the wrong place.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"

FORBIDDEN_PATTERNS = [
    ".ai/memory/coder",
    ".ai/memory/{agent}",
    ".ai/memory/tester",
    ".ai/memory/heartbeat",
]


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return (line_number, line) for lines matching forbidden patterns."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in line:
                hits.append((i, line.strip()))
                break
    return hits


class TestNoDeprecatedMemoryPaths:
    def test_claude_md_clean(self) -> None:
        path = CLAUDE_DIR / "CLAUDE.md"
        hits = _scan_file(path)
        assert not hits, f"~/.claude/CLAUDE.md references deprecated paths:\n" + "\n".join(
            f"  L{n}: {line}" for n, line in hits
        )

    def test_settings_json_clean(self) -> None:
        path = CLAUDE_DIR / "settings.json"
        if not path.exists():
            pytest.skip("settings.json not found")
        hits = _scan_file(path)
        assert not hits, f"settings.json references deprecated paths:\n" + "\n".join(
            f"  L{n}: {line}" for n, line in hits
        )

    def test_hooks_clean(self) -> None:
        hooks_dir = CLAUDE_DIR / "hooks"
        if not hooks_dir.exists():
            pytest.skip("hooks dir not found")
        all_hits: dict[str, list[tuple[int, str]]] = {}
        for f in hooks_dir.iterdir():
            if f.suffix in (".py", ".sh", ".js"):
                hits = _scan_file(f)
                if hits:
                    all_hits[f.name] = hits
        assert not all_hits, "Hook scripts reference deprecated paths:\n" + "\n".join(
            f"  {name}:\n" + "\n".join(f"    L{n}: {line}" for n, line in hits)
            for name, hits in all_hits.items()
        )

    def test_reflect_skill_clean(self) -> None:
        path = CLAUDE_DIR / "skills" / "reflect" / "SKILL.md"
        hits = _scan_file(path)
        assert not hits, f"reflect SKILL.md references deprecated paths:\n" + "\n".join(
            f"  L{n}: {line}" for n, line in hits
        )


class TestActiveProjectCLAUDEMDs:
    """Scan project CLAUDE.md files for deprecated memory instructions."""

    PROJECTS_ROOT = Path("C:/Projects")

    def _project_claude_md(self, name: str) -> Path:
        return self.PROJECTS_ROOT / name / "CLAUDE.md"

    @pytest.fixture(params=["beautiful-tree", "svapna", "mooduel", "seeker-ai"])
    def project_name(self, request: pytest.FixtureRequest) -> str:
        return request.param

    def test_no_deprecated_memory_refs(self, project_name: str) -> None:
        path = self._project_claude_md(project_name)
        if not path.exists():
            pytest.skip(f"{project_name}/CLAUDE.md not found")
        hits = _scan_file(path)
        assert not hits, (
            f"{project_name}/CLAUDE.md references deprecated paths:\n"
            + "\n".join(f"  L{n}: {line}" for n, line in hits)
        )


class TestSmritiMCPRegistered:
    """Verify smriti MCP server is registered in ~/.claude.json."""

    def test_mcp_server_registered(self) -> None:
        config = HOME / ".claude.json"
        if not config.exists():
            pytest.skip("~/.claude.json not found")
        data = json.loads(config.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        assert "smriti" in servers, "smriti MCP server not registered in ~/.claude.json"
        assert servers["smriti"].get("command") == "python"
        assert "-m" in servers["smriti"].get("args", [])
        assert "smriti.mcp_server" in servers["smriti"].get("args", [])


class TestWakeHookWired:
    """Verify the SessionStart hook calls wake.py with NARADA_WAKE=1."""

    def test_session_start_hook_exists(self) -> None:
        settings = CLAUDE_DIR / "settings.json"
        if not settings.exists():
            pytest.skip("settings.json not found")
        data = json.loads(settings.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        session_start = hooks.get("SessionStart", [])

        wake_found = False
        for group in session_start:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "wake.py" in cmd and "NARADA_WAKE" in cmd:
                    wake_found = True
                    break

        assert wake_found, "SessionStart hook does not call wake.py with NARADA_WAKE"
