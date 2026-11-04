"""What the search index covers: one policy for bulk and single-file indexing.

``index_tree`` and ``index_file`` both ask :func:`is_indexable`, and the purge
pass in ``index_tree`` drops chunks of any source that's gone from disk or
that the policy now excludes, so tightening the policy also cleans indexes
built before it.

Excluded, and why (retrieval eval, 2026-11-04):

- ``.smriti/``, ``.git/``: system state, not memory.
- ``private/``: the encrypted private store; its README holds the key.
- ``heartbeat/``: raw output of the retired heartbeat loop and raw channel
  transcripts. It was 34% of chunks and never once a helpful hit; the
  nightly digests carry what mattered.
- ``mirrors/``: junctions to other repos' docs, which duplicate their
  sources (19% of chunks) and crowd out the memory itself. Except
  ``mirrors/<project>/findings/`` and ``decisions/``: records that smriti's
  own MCP tools write (``smriti_record_finding``, ``smriti_record_decision``),
  which are memory and must stay searchable.
- ``log/YYYY/MM/DD.md``: raw day-log renders (26% of chunks, one 13.8 MB
  day alone). The day's digest and rollups are indexed instead.
"""

from __future__ import annotations

import re

EXCLUDED_TOP_LEVEL = frozenset({".smriti", ".git", "private", "heartbeat", "mirrors"})
# Same as the scanner's default (memsearch.scanner.scan_paths), compared case-insensitively.
MARKDOWN_SUFFIXES = (".md", ".markdown")
_RAW_DAY_RENDER = re.compile(r"^log/\d{4}/\d{2}/\d{2}\.md$")
_MIRROR_RECORDS = re.compile(r"^mirrors/[^/]+/(findings|decisions)/", re.IGNORECASE)


def is_indexable(rel_source: str) -> bool:
    """Whether a tree-relative path (``/`` or ``\\`` separated) belongs in the index."""
    rel = rel_source.replace("\\", "/").strip("/")
    if not rel.lower().endswith(MARKDOWN_SUFFIXES):
        return False
    top = rel.split("/", 1)[0]
    if top == "mirrors":
        return bool(_MIRROR_RECORDS.match(rel))
    if top in EXCLUDED_TOP_LEVEL:
        return False
    return not _RAW_DAY_RENDER.match(rel.lower())
