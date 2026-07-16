"""Tests for src/smriti/integrations/common/managed_doc.py.

The managed block is the guarantee that installers can never destroy
user-authored agent-doc content. Adversarial review of the plan
(2026-07-16/17) demanded: fail-closed on unmarked/malformed files
BEFORE any mutation, one-time migration recognizing legacy generated
sections by heading, byte-for-byte preservation outside the block, and
atomic writes.
"""

from __future__ import annotations

import pytest

from smriti.integrations.common.managed_doc import (
    MANAGED_BEGIN,
    MANAGED_END,
    ManagedDocError,
    classify_doc,
    migrate_agent_doc,
    write_managed_doc,
)

BLOCK_V1 = "## Memory system\ncontract v1\n"
BLOCK_V2 = "## Memory system\ncontract v2 — updated\n"


# A realistic legacy doc: user sections interleaved with generated ones
# (mirrors the live ~/.claude/CLAUDE.md shape found in the audit).
LEGACY_DOC = """# CLAUDE.md (user-global)

## Asking questions

Ask inline as a numbered list. Personal preference, hands off.

## Memory system — smriti is the single write path

Old generated contract text.

### When to write

Old generated sub-section.

## Memory search — prefer smriti_read over Grep

Old generated search guidance.

## Plan review — my personal policy

User-authored section between generated ones stays put.

## Session wake

Old generated wake section.
"""


class TestClassifyDoc:
    def test_missing(self, tmp_path):
        assert classify_doc(tmp_path / "nope.md").kind == "missing"

    def test_managed(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_text(f"before\n{MANAGED_BEGIN}\nx\n{MANAGED_END}\nafter\n", encoding="utf-8")
        assert classify_doc(p).kind == "managed"

    def test_migration_required(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_text("# Just some text, no markers\n", encoding="utf-8")
        state = classify_doc(p)
        assert state.kind == "migration-required"
        assert "--migrate-agent-doc" in state.detail

    def test_duplicate_markers_malformed(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_text(
            f"{MANAGED_BEGIN}\na\n{MANAGED_END}\n{MANAGED_BEGIN}\nb\n{MANAGED_END}\n",
            encoding="utf-8",
        )
        assert classify_doc(p).kind == "malformed"

    def test_reversed_markers_malformed(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_text(f"{MANAGED_END}\nx\n{MANAGED_BEGIN}\n", encoding="utf-8")
        assert classify_doc(p).kind == "malformed"


class TestWriteManagedDoc:
    def test_fresh_file_gets_title_and_block(self, tmp_path):
        p = tmp_path / "doc.md"
        changed = write_managed_doc(p, BLOCK_V1, title="# CLAUDE.md (user-global)")
        assert changed
        text = p.read_text(encoding="utf-8")
        assert text.startswith("# CLAUDE.md (user-global)\n\n")
        assert MANAGED_BEGIN in text and MANAGED_END in text
        assert "contract v1" in text

    def test_rewrite_is_idempotent(self, tmp_path):
        p = tmp_path / "doc.md"
        write_managed_doc(p, BLOCK_V1)
        assert write_managed_doc(p, BLOCK_V1) is False

    def test_rewrite_preserves_user_content_byte_for_byte(self, tmp_path):
        p = tmp_path / "doc.md"
        before = "# Title\n\nuser prose ünïcode\n\n"
        after = "\n\n## User section\n\ntrailing prose\n"
        p.write_text(
            before + f"{MANAGED_BEGIN}\n{BLOCK_V1}\n{MANAGED_END}" + after,
            encoding="utf-8",
        )

        assert write_managed_doc(p, BLOCK_V2)

        text = p.read_text(encoding="utf-8")
        assert text.startswith(before)
        assert text.endswith(after)
        assert "contract v2" in text
        assert "contract v1" not in text

    def test_refuses_unmarked_file(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_text("no markers here\n", encoding="utf-8")
        with pytest.raises(ManagedDocError):
            write_managed_doc(p, BLOCK_V1)
        assert p.read_text(encoding="utf-8") == "no markers here\n"

    def test_refuses_malformed_file(self, tmp_path):
        p = tmp_path / "doc.md"
        original = f"{MANAGED_BEGIN}\na\n{MANAGED_END}\n{MANAGED_BEGIN}\nb\n{MANAGED_END}\n"
        p.write_text(original, encoding="utf-8")
        with pytest.raises(ManagedDocError):
            write_managed_doc(p, BLOCK_V1)
        assert p.read_text(encoding="utf-8") == original

    def test_no_tmp_file_left_behind(self, tmp_path):
        p = tmp_path / "doc.md"
        write_managed_doc(p, BLOCK_V1)
        assert not (tmp_path / "doc.md.tmp").exists()


class TestMigrateAgentDoc:
    def test_replaces_legacy_sections_preserves_user_sections(self, tmp_path):
        p = tmp_path / "CLAUDE.md"
        p.write_text(LEGACY_DOC, encoding="utf-8")

        backup = migrate_agent_doc(p, BLOCK_V1)

        text = p.read_text(encoding="utf-8")
        # User content survives, in order.
        assert "# CLAUDE.md (user-global)" in text
        assert "## Asking questions" in text
        assert "## Plan review — my personal policy" in text
        # Legacy generated sections are gone (including ### children).
        assert "Old generated contract text." not in text
        assert "Old generated sub-section." not in text
        assert "Old generated search guidance." not in text
        assert "Old generated wake section." not in text
        # Exactly one marker pair, at the first legacy section's slot
        # (after "Asking questions", before "Plan review").
        assert text.count(MANAGED_BEGIN) == 1
        assert text.index("Asking questions") < text.index(MANAGED_BEGIN)
        assert text.index(MANAGED_BEGIN) < text.index("Plan review")
        # Backup holds the pre-migration bytes.
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == LEGACY_DOC

    def test_migrated_doc_is_managed_and_rewritable(self, tmp_path):
        p = tmp_path / "CLAUDE.md"
        p.write_text(LEGACY_DOC, encoding="utf-8")
        migrate_agent_doc(p, BLOCK_V1)

        assert classify_doc(p).kind == "managed"
        assert write_managed_doc(p, BLOCK_V2)
        text = p.read_text(encoding="utf-8")
        assert "contract v2" in text
        assert "## Asking questions" in text

    def test_no_legacy_sections_appends_block(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_text("# Mine\n\n## Only user stuff\n\nprose\n", encoding="utf-8")
        migrate_agent_doc(p, BLOCK_V1)
        text = p.read_text(encoding="utf-8")
        assert text.startswith("# Mine\n\n## Only user stuff\n\nprose\n")
        assert text.rstrip().endswith(MANAGED_END)

    def test_refuses_already_managed(self, tmp_path):
        p = tmp_path / "doc.md"
        write_managed_doc(p, BLOCK_V1)
        with pytest.raises(ManagedDocError, match="already"):
            migrate_agent_doc(p, BLOCK_V1)

    def test_refuses_missing_and_malformed(self, tmp_path):
        with pytest.raises(ManagedDocError):
            migrate_agent_doc(tmp_path / "nope.md", BLOCK_V1)
        p = tmp_path / "bad.md"
        p.write_text(f"{MANAGED_END}\n{MANAGED_BEGIN}\n", encoding="utf-8")
        with pytest.raises(ManagedDocError):
            migrate_agent_doc(p, BLOCK_V1)
