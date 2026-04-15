"""Tests for the routing module."""

from __future__ import annotations

from pathlib import Path

import pytest

from smriti.store.router import (
    RoutingAction,
    RoutingResult,
    _parse_routing_response,
    execute_link,
    execute_task,
    routing_judge_auto_skip,
)


# ── Dataclass tests ─────────────────────────────────────────────────


def test_routing_action_fields():
    action = RoutingAction(
        action="REVISE",
        target="concepts/viveka.md",
        direction="add new distinction",
        reason="new info about viveka",
    )
    assert action.action == "REVISE"
    assert action.target == "concepts/viveka.md"


def test_routing_result_defaults():
    result = RoutingResult()
    assert result.actions == []
    assert result.meta.model == ""


# ── JSON parsing ────────────────────────────────────────────────────


def test_parse_routing_response_valid():
    raw = """[
        {"action": "REVISE", "target": "concepts/memory.md", "direction": "add X", "reason": "new info"},
        {"action": "LINK", "target": "threads/design.md", "direction": "related", "reason": "topical"}
    ]"""
    actions = _parse_routing_response(raw)
    assert len(actions) == 2
    assert actions[0].action == "REVISE"
    assert actions[0].target == "concepts/memory.md"
    assert actions[1].action == "LINK"


def test_parse_routing_response_with_preamble():
    raw = """Here are my routing decisions:
    [{"action": "TASK", "target": "goals/Q2.md", "direction": "review paper", "reason": "relevant"}]
    That's all."""
    actions = _parse_routing_response(raw)
    assert len(actions) == 1
    assert actions[0].action == "TASK"


def test_parse_routing_response_empty_array():
    actions = _parse_routing_response("[]")
    assert actions == []


def test_parse_routing_response_malformed():
    actions = _parse_routing_response("this is not json at all")
    assert actions == []


def test_parse_routing_response_unknown_action():
    raw = '[{"action": "DESTROY", "target": "x.md", "direction": "", "reason": ""}]'
    actions = _parse_routing_response(raw)
    assert actions == []  # unknown actions are skipped


def test_parse_routing_response_case_insensitive():
    raw = '[{"action": "revise", "target": "x.md", "direction": "fix", "reason": "y"}]'
    actions = _parse_routing_response(raw)
    assert len(actions) == 1
    assert actions[0].action == "REVISE"


# ── Test stub ───────────────────────────────────────────────────────


def test_routing_judge_auto_skip():
    result = routing_judge_auto_skip("some content", [{"source": "x.md", "content": "y"}])
    assert result.actions == []


# ── execute_link ────────────────────────────────────────────────────


def test_execute_link_adds_wikilink(tmp_path: Path):
    summary = tmp_path / "sources" / "2026" / "04-14-001.md"
    summary.parent.mkdir(parents=True)
    summary.write_text("# Summary\n\nSome content here.\n", encoding="utf-8")

    target = tmp_path / "concepts" / "viveka.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Viveka\n", encoding="utf-8")

    changed = execute_link(summary, target, tmp_path)
    assert changed is True

    content = summary.read_text(encoding="utf-8")
    assert "[[concepts/viveka]]" in content
    assert "## Related" in content


def test_execute_link_deduplicates(tmp_path: Path):
    summary = tmp_path / "summary.md"
    summary.write_text(
        "# Summary\n\n## Related\n\n- [[concepts/viveka]]\n",
        encoding="utf-8",
    )

    target = tmp_path / "concepts" / "viveka.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Viveka\n", encoding="utf-8")

    changed = execute_link(summary, target, tmp_path)
    assert changed is False  # already linked


def test_execute_link_appends_to_existing_section(tmp_path: Path):
    summary = tmp_path / "summary.md"
    summary.write_text(
        "# Summary\n\n## Related\n\n- [[concepts/dharma]]\n",
        encoding="utf-8",
    )

    target = tmp_path / "concepts" / "viveka.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Viveka\n", encoding="utf-8")

    changed = execute_link(summary, target, tmp_path)
    assert changed is True

    content = summary.read_text(encoding="utf-8")
    assert "[[concepts/viveka]]" in content
    assert "[[concepts/dharma]]" in content
    # Should only have one Related section
    assert content.count("## Related") == 1


# ── execute_task ────────────────────────────────────────────────────


def test_execute_task_creates_section(tmp_path: Path):
    target = tmp_path / "goals" / "Q2.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Q2 Goals\n\nShip smriti v0.1.\n", encoding="utf-8")

    summary = tmp_path / "sources" / "paper.md"
    summary.parent.mkdir(parents=True)
    summary.write_text("# Paper\n", encoding="utf-8")

    changed = execute_task(target, "Review paper for architecture implications", summary, tmp_path)
    assert changed is True

    content = target.read_text(encoding="utf-8")
    assert "## Tasks" in content
    assert "Review paper for architecture implications" in content
    assert "[[sources/paper]]" in content


def test_execute_task_appends_to_existing_section(tmp_path: Path):
    target = tmp_path / "goals.md"
    target.write_text(
        "# Goals\n\n## Tasks\n\n- [ ] Existing task\n",
        encoding="utf-8",
    )

    summary = tmp_path / "source.md"
    summary.write_text("# Source\n", encoding="utf-8")

    changed = execute_task(target, "New task from ingest", summary, tmp_path)
    assert changed is True

    content = target.read_text(encoding="utf-8")
    assert "Existing task" in content
    assert "New task from ingest" in content


def test_execute_task_deduplicates(tmp_path: Path):
    target = tmp_path / "goals.md"
    target.write_text(
        "# Goals\n\n## Tasks\n\n- [ ] Already here\n",
        encoding="utf-8",
    )

    summary = tmp_path / "source.md"
    summary.write_text("# Source\n", encoding="utf-8")

    changed = execute_task(target, "Already here", summary, tmp_path)
    assert changed is False
