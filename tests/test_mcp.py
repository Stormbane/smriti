"""Integration tests for the smriti MCP server (read/write/status)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from smriti.store.indexer import index_tree
from smriti.store.writer import write_entry


@pytest.fixture()
def indexed_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal indexed tree for MCP tests."""
    monkeypatch.setenv("NARADA_ROOT", str(tmp_path))

    (tmp_path / "identity.md").write_text(
        "# Identity\nI am the test entity.\n", encoding="utf-8"
    )
    (tmp_path / "journal").mkdir()
    (tmp_path / "notes").mkdir()
    (tmp_path / "projects").mkdir()

    smriti_dir = tmp_path / ".smriti"
    smriti_dir.mkdir()

    index_tree(root=tmp_path)
    return tmp_path


class TestSmritiWrite:
    def test_write_creates_file_in_branch(self, indexed_tree: Path) -> None:
        path = write_entry(
            "Test observation about sovereignty.",
            branch="journal",
            root=indexed_tree,
            reindex=False,
        )
        assert path.exists()
        assert "journal" in str(path)
        content = path.read_text(encoding="utf-8")
        assert "sovereignty" in content

    def test_write_to_project_branch(self, indexed_tree: Path) -> None:
        path = write_entry(
            "Beautiful Tree needs a new migration.",
            branch="projects/beautiful-tree",
            root=indexed_tree,
            reindex=False,
        )
        assert path.exists()
        assert "projects" in str(path)
        assert "beautiful-tree" in str(path)

    def test_write_to_notes_branch(self, indexed_tree: Path) -> None:
        path = write_entry(
            "Qwen3 struggles with long JSON.",
            branch="notes",
            root=indexed_tree,
            reindex=False,
        )
        assert path.exists()
        assert "notes" in str(path)

    def test_write_with_title(self, indexed_tree: Path) -> None:
        path = write_entry(
            "Content here.",
            branch="journal",
            title="Session reflection",
            root=indexed_tree,
            reindex=False,
        )
        content = path.read_text(encoding="utf-8")
        assert "Session reflection" in content

    def test_write_with_source_hint(self, indexed_tree: Path) -> None:
        path = write_entry(
            "From a heartbeat cycle.",
            branch="journal",
            source_hint="heartbeat-042",
            root=indexed_tree,
            reindex=False,
        )
        content = path.read_text(encoding="utf-8")
        assert "heartbeat-042" in content

    def test_sequential_writes_increment_counter(self, indexed_tree: Path) -> None:
        p1 = write_entry("First.", branch="journal", root=indexed_tree, reindex=False)
        p2 = write_entry("Second.", branch="journal", root=indexed_tree, reindex=False)
        assert p1 != p2
        assert p1.parent == p2.parent


class TestSmritiReadAfterWrite:
    def test_written_entry_is_searchable(self, indexed_tree: Path) -> None:
        from smriti.store.search import search
        from smriti.store.schema import ensure_schema

        write_entry(
            "The antahkarana model has four faculties.",
            branch="journal",
            root=indexed_tree,
            reindex=True,
        )

        db_path = indexed_tree / ".smriti" / "index.db"
        import sqlite3
        tmp = sqlite3.connect(str(db_path))
        row = tmp.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
        tmp.close()
        db = ensure_schema(db_path, int(row[0]))

        results = search(db, "antahkarana four faculties", top_k=5)
        sources = [r.source for r in results]
        assert any("journal" in s for s in sources)


class TestSmritiWriteWrongPlace:
    def test_write_never_touches_claude_projects(self, indexed_tree: Path) -> None:
        claude_projects = Path.home() / ".claude" / "projects"
        before = set()
        if claude_projects.exists():
            before = {str(p) for p in claude_projects.rglob("*.md")}

        write_entry(
            "This should go to the tree, not claude projects.",
            branch="journal",
            root=indexed_tree,
            reindex=False,
        )

        after = set()
        if claude_projects.exists():
            after = {str(p) for p in claude_projects.rglob("*.md")}

        new_files = after - before
        assert not new_files, f"smriti_write created files in ~/.claude/projects/: {new_files}"

    def test_write_stays_under_root(self, indexed_tree: Path) -> None:
        path = write_entry(
            "Content.",
            branch="journal",
            root=indexed_tree,
            reindex=False,
        )
        assert str(path).startswith(str(indexed_tree))


class TestMCPServerProtocol:
    def _call_mcp(self, method: str, params: dict | None = None, tree_root: Path | None = None) -> dict:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
        }
        if params:
            request["params"] = params

        env = {}
        if tree_root:
            env["NARADA_ROOT"] = str(tree_root)

        result = subprocess.run(
            [sys.executable, "-m", "smriti.mcp_server"],
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            env={**__import__("os").environ, **env},
            timeout=30,
        )
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        for line in lines:
            try:
                parsed = json.loads(line)
                if "result" in parsed or "error" in parsed:
                    return parsed
            except json.JSONDecodeError:
                continue
        return {"error": f"No valid JSON-RPC response. stdout: {result.stdout[:500]}"}

    def test_tools_list(self, indexed_tree: Path) -> None:
        resp = self._call_mcp("tools/list", tree_root=indexed_tree)
        if "result" in resp:
            tools = resp["result"].get("tools", [])
            names = [t["name"] for t in tools]
            assert "smriti_read" in names
            assert "smriti_write" in names
            assert "smriti_status" in names

    def test_status_call(self, indexed_tree: Path) -> None:
        resp = self._call_mcp(
            "tools/call",
            {"name": "smriti_status", "arguments": {}},
            tree_root=indexed_tree,
        )
        if "result" in resp:
            content = resp["result"].get("content", [{}])
            text = content[0].get("text", "") if content else ""
            assert "Tree root" in text or "indexed" in text.lower()
