"""Tests for index.md maintenance (smriti.store.index_pages)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from smriti.store.index_pages import update_index_pages


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A tree whose notes/ and notes/projects/ directories keep an index.md."""
    (tmp_path / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
    (tmp_path / "index.md").write_text("# Root index\n", encoding="utf-8")
    projects = tmp_path / "notes" / "projects"
    projects.mkdir(parents=True)
    (tmp_path / "notes" / "index.md").write_text("# Notes\n", encoding="utf-8")
    (projects / "index.md").write_text("# Projects\n", encoding="utf-8")
    (projects / "lantern.md").write_text("# Lantern\n\nOffline inventory.\n", encoding="utf-8")
    (tmp_path / "people").mkdir()
    (tmp_path / ".smriti").mkdir()
    return tmp_path


def test_a_new_file_is_listed_by_its_heading(tree: Path) -> None:
    projects = tree / "notes" / "projects"
    kite = projects / "kite.md"
    kite.write_text("# Kite repairs\n\nCaptive screws.\n", encoding="utf-8")

    updated = update_index_pages(kite, tree)

    assert updated == [projects / "index.md", tree / "notes" / "index.md"]
    listing = (projects / "index.md").read_text(encoding="utf-8")
    assert "- [[notes/projects/kite]] — Kite repairs" in listing
    assert "- [[notes/projects/lantern]] — Lantern" in listing
    assert "index.md" not in listing
    assert "- [[notes/projects/]] — projects" in (tree / "notes" / "index.md").read_text(
        encoding="utf-8"
    )


def test_the_root_and_unindexed_directories_are_left_alone(tree: Path) -> None:
    sam = tree / "people" / "sam.md"
    sam.write_text("# Sam\n", encoding="utf-8")

    assert update_index_pages(sam, tree) == []
    assert not (tree / "people" / "index.md").exists()
    assert (tree / "index.md").read_text(encoding="utf-8") == "# Root index\n"
    assert (tree / "MEMORY.md").read_text(encoding="utf-8") == "# Root\n"


def test_an_unchanged_listing_is_not_rewritten(tree: Path) -> None:
    lantern = tree / "notes" / "projects" / "lantern.md"
    assert update_index_pages(lantern, tree)
    assert update_index_pages(lantern, tree) == []


def test_concurrent_writers_in_one_directory_are_all_listed(tree: Path) -> None:
    # The listing is taken under the index's edit lock, so whichever writer
    # rewrites the index last has seen every file written before it.
    projects = tree / "notes" / "projects"
    n = 8
    start = threading.Barrier(n)
    errors: list[BaseException] = []

    def write(i: int) -> None:
        start.wait()
        try:
            page = projects / f"part-{i}.md"
            page.write_text(f"# Part {i}\n", encoding="utf-8")
            update_index_pages(page, tree)
        except BaseException as exc:  # noqa: BLE001 — collected, asserted on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    listing = (projects / "index.md").read_text(encoding="utf-8")
    for i in range(n):
        assert f"- [[notes/projects/part-{i}]] — Part {i}" in listing


def test_write_entry_refreshes_the_parent_index(tree: Path) -> None:
    from smriti.store.writer import write_entry

    entry = write_entry("A note.", branch="notes", title="Bench", root=tree, reindex=False)

    year = entry.parent.name
    listing = (tree / "notes" / "index.md").read_text(encoding="utf-8")
    assert f"- [[notes/{year}/]] — {year}" in listing
