"""Tests for cascade, queue, and judge modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from smriti.store.cascade import (
    cognitive_cascade,
    find_upstream_references,
    queue_cognitive_cascade,
    structural_cascade,
)
from smriti.store.judge import (
    JudgmentResult,
    executor_echo,
    judge_auto_keep,
)
from smriti.store.queue import (
    QueueTask,
    cleanup,
    dequeue,
    enqueue,
    pending_count,
    queue_summary,
)


@pytest.fixture()
def cascade_tree(tmp_path: Path) -> Path:
    """Create a tree with index.md files for cascade testing."""
    # Root
    (tmp_path / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
    (tmp_path / "identity.md").write_text("# Identity\n", encoding="utf-8")

    # Threads with index
    threads = tmp_path / "threads"
    threads.mkdir()
    (threads / "index.md").write_text(
        "# Threads\n\n- [[threads/memory-design]] — memory system\n",
        encoding="utf-8",
    )
    (threads / "memory-design.md").write_text(
        "# Memory Design\n\nReferences [[semantic/concepts/viveka]].\n",
        encoding="utf-8",
    )

    # Concepts with index
    concepts = tmp_path / "semantic" / "concepts"
    concepts.mkdir(parents=True)
    (concepts / "index.md").write_text(
        "# Concepts\n\n- [[semantic/concepts/viveka]] — discrimination\n",
        encoding="utf-8",
    )
    (concepts / "viveka.md").write_text(
        "# Viveka\n\nDiscrimination as a faculty.\n",
        encoding="utf-8",
    )

    # .smriti directory (should be ignored)
    smriti_dir = tmp_path / ".smriti"
    smriti_dir.mkdir()

    return tmp_path


def test_structural_cascade_updates_index(cascade_tree: Path) -> None:
    """Adding a file triggers parent index.md update."""
    concepts = cascade_tree / "semantic" / "concepts"
    new_file = concepts / "sovereignty.md"
    new_file.write_text("# Sovereignty\n\nLoRA always-on.\n", encoding="utf-8")

    updated = structural_cascade(new_file, cascade_tree)

    assert len(updated) >= 1
    # The concepts/index.md should have been updated
    index_content = (concepts / "index.md").read_text(encoding="utf-8")
    assert "sovereignty" in index_content.lower()


def test_structural_cascade_stops_at_root(cascade_tree: Path) -> None:
    """Cascade does not touch MEMORY.md at root."""
    concepts = cascade_tree / "semantic" / "concepts"
    new_file = concepts / "test.md"
    new_file.write_text("# Test\n", encoding="utf-8")

    updated = structural_cascade(new_file, cascade_tree)

    updated_names = [p.name for p in updated]
    assert "MEMORY.md" not in updated_names


def test_find_upstream_references(cascade_tree: Path) -> None:
    """Files referencing a path via wikilinks are found."""
    viveka = cascade_tree / "semantic" / "concepts" / "viveka.md"
    refs = find_upstream_references(viveka, cascade_tree)

    # memory-design.md references viveka
    ref_names = [r.name for r in refs]
    assert "memory-design.md" in ref_names


def test_cognitive_cascade_auto_keep(cascade_tree: Path) -> None:
    """Cognitive cascade with auto_keep judge stops immediately."""
    viveka = cascade_tree / "semantic" / "concepts" / "viveka.md"

    stats = cognitive_cascade(
        viveka,
        cascade_tree,
        judge_fn=judge_auto_keep,
        executor_fn=executor_echo,
    )

    # All verdicts should be KEEP
    for v in stats["verdicts"]:
        assert v["verdict"] == "KEEP"
    assert len(stats["files_changed"]) == 0


def test_cognitive_cascade_with_revise(cascade_tree: Path) -> None:
    """Cognitive cascade with a revising judge updates the parent."""

    def judge_always_revise(parent: str, child: str, prompt: Path | None = None) -> JudgmentResult:
        return JudgmentResult(
            seeing="Test: always revise.",
            verdict="REVISE",
            direction="Add a note about the change.",
            reason="Testing.",
        )

    def executor_append_note(parent: str, direction: str, child: str, prompt: Path | None = None) -> str:
        return parent + "\n\n*Note: updated by cascade test.*\n"

    viveka = cascade_tree / "semantic" / "concepts" / "viveka.md"

    stats = cognitive_cascade(
        viveka,
        cascade_tree,
        judge_fn=judge_always_revise,
        executor_fn=executor_append_note,
    )

    assert len(stats["files_changed"]) >= 1
    # memory-design.md should have been revised
    memory_design = cascade_tree / "threads" / "memory-design.md"
    content = memory_design.read_text(encoding="utf-8")
    assert "updated by cascade test" in content


def test_cognitive_cascade_trunk_protection(cascade_tree: Path) -> None:
    """Cascade reaching trunk files flags for human review, doesn't auto-apply."""
    # Set up chain: identity.md references threads/index → when threads/index
    # changes, cascade reaches identity.md and should PROMOTE, not REVISE.
    (cascade_tree / "identity.md").write_text(
        "# Identity\n\nSee [[threads/index]] for active work.\n",
        encoding="utf-8",
    )

    def judge_always_revise(parent: str, child: str, prompt: Path | None = None) -> JudgmentResult:
        return JudgmentResult(
            seeing="Test.", verdict="REVISE",
            direction="Update.", reason="Test.",
        )

    def executor_append(parent: str, direction: str, child: str, prompt: Path | None = None) -> str:
        return parent + "\n*cascade touched this*\n"

    # Cascade starts from threads/index.md being changed
    threads_index = cascade_tree / "threads" / "index.md"
    stats = cognitive_cascade(
        threads_index,
        cascade_tree,
        judge_fn=judge_always_revise,
        executor_fn=executor_append,
    )

    # identity.md should be in promoted (not changed) because it's a trunk file
    assert any("identity.md" in p for p in stats["promoted"])
    # identity.md should NOT have been modified
    identity_content = (cascade_tree / "identity.md").read_text(encoding="utf-8")
    assert "cascade touched this" not in identity_content


def test_cognitive_cascade_cycle_protection(tmp_path: Path) -> None:
    """Cascade with a wikilink cycle A → B → C → A doesn't re-revise files."""
    # Build a cyclic reference graph
    (tmp_path / "a.md").write_text("# A\n\nRefs [[b]].\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n\nRefs [[c]].\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("# C\n\nRefs [[a]].\n", encoding="utf-8")

    revise_counts: dict[str, int] = {}

    def counting_judge(parent: str, child: str, prompt: Path | None = None) -> JudgmentResult:
        return JudgmentResult(
            seeing="Test.", verdict="REVISE",
            direction="Update.", reason="Test cycle.",
        )

    def counting_executor(parent: str, direction: str, child: str, prompt: Path | None = None) -> str:
        # Count how many times this file is revised by inspecting the child arg
        revise_counts[child[:20]] = revise_counts.get(child[:20], 0) + 1
        return parent + "\n*revised*\n"

    # Start cascade from a.md — with a cycle, each file should be revised at most once
    stats = cognitive_cascade(
        tmp_path / "a.md",
        tmp_path,
        judge_fn=counting_judge,
        executor_fn=counting_executor,
    )

    # Each unique file should appear in files_changed at most once
    assert len(stats["files_changed"]) == len(set(stats["files_changed"]))
    # With cycle protection, the max_depth is bounded by graph size, not MAX_CASCADE_DEPTH
    assert stats["max_depth"] < 5


def test_protected_files_env_override(tmp_path: Path, monkeypatch) -> None:
    """NARADA_PROTECTED_FILES env var overrides the default set."""
    import importlib

    monkeypatch.setenv("NARADA_PROTECTED_FILES", "my-special.md, another.md")

    # Reload cascade module so the env var is picked up
    import smriti.store.cascade as cascade_mod
    importlib.reload(cascade_mod)

    assert "my-special.md" in cascade_mod.PROTECTED_FILES
    assert "another.md" in cascade_mod.PROTECTED_FILES
    assert "identity.md" not in cascade_mod.PROTECTED_FILES

    # Reload with env cleared to restore defaults for other tests
    monkeypatch.delenv("NARADA_PROTECTED_FILES")
    importlib.reload(cascade_mod)
    assert "identity.md" in cascade_mod.PROTECTED_FILES


# ── Queue tests ──────────────────────────────────────────────────────


def test_queue_enqueue_dequeue(tmp_path: Path) -> None:
    """Basic enqueue and dequeue."""
    task = QueueTask(type="reindex", path="test.md", priority=5)
    enqueue(task, root=tmp_path)

    assert pending_count(root=tmp_path) == 1

    tasks = dequeue(1, root=tmp_path)
    assert len(tasks) == 1
    assert tasks[0].type == "reindex"
    assert tasks[0].path == "test.md"


def test_queue_dedup(tmp_path: Path) -> None:
    """Duplicate pending tasks are not added."""
    task1 = QueueTask(type="reindex", path="test.md")
    task2 = QueueTask(type="reindex", path="test.md")
    enqueue(task1, root=tmp_path)
    enqueue(task2, root=tmp_path)

    assert pending_count(root=tmp_path) == 1


def test_queue_cleanup(tmp_path: Path) -> None:
    """Cleanup removes done/failed tasks."""
    from smriti.store.queue import complete

    t1 = QueueTask(type="reindex", path="a.md")
    t2 = QueueTask(type="reindex", path="b.md")
    enqueue(t1, root=tmp_path)
    enqueue(t2, root=tmp_path)

    tasks = dequeue(2, root=tmp_path)
    for t in tasks:
        complete(t.id, root=tmp_path)

    assert pending_count(root=tmp_path) == 0
    removed = cleanup(root=tmp_path)
    assert removed == 2


def test_queue_summary(tmp_path: Path) -> None:
    """Summary shows counts by status."""
    enqueue(QueueTask(type="a", path="1.md"), root=tmp_path)
    enqueue(QueueTask(type="b", path="2.md"), root=tmp_path)
    dequeue(1, root=tmp_path)  # marks one as processing

    summary = queue_summary(root=tmp_path)
    assert summary.get("pending", 0) == 1
    assert summary.get("processing", 0) == 1
