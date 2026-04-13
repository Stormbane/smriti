"""Labeled test cases for evaluating smriti's JUDGE, search, and cascade.

Each case has a known-correct outcome. The runner executes them against the
current system and compares results to expected values.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class JudgeCase:
    """A JUDGE eval case with known-correct verdict."""

    id: str
    description: str
    parent_content: str
    child_content: str
    expected_verdict: str  # KEEP | REVISE | REJECT | PROMOTE
    expected_direction_keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class SearchCase:
    """A search quality case with known-relevant results."""

    id: str
    description: str
    query: str
    expected_in_top_k: list[str]  # source substrings that MUST appear in top-k
    expected_not_in: list[str] = field(default_factory=list)
    k: int = 5


@dataclass
class CascadeCase:
    """A cascade behavior case with expected depth/verdict pattern."""

    id: str
    description: str
    trigger_file: str
    trigger_content: str
    expected_max_depth: int
    expected_trunk_flag: bool = False
    tags: list[str] = field(default_factory=list)


# ── JUDGE cases (~15) ────────────────────────────────────────────────


# The original cases in this version referenced private memory and were removed
# from the public history on 2026-11-04. The current src/smriti/eval/cases.py
# holds synthetic cases.
JUDGE_CASES: list[JudgeCase] = []
SEARCH_CASES: list[SearchCase] = []
CASCADE_CASES: list[CascadeCase] = []
