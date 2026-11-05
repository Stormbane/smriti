"""Labeled search cases for evaluating smriti's retrieval.

Each case has a known-correct outcome. The runner executes them against the
bundled fictional Ari/Sam fixture and compares results to expected values.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchCase:
    """A search quality case with known-relevant results."""

    id: str
    description: str
    query: str
    expected_in_top_k: list[str]  # source substrings that MUST appear in top-k
    expected_not_in: list[str] = field(default_factory=list)
    k: int = 5


# All text below is invented. No personal memory is used in these cases.

SEARCH_CASES: list[SearchCase] = [
    SearchCase("s01-identity", "Agent identity", "Who is Ari?", ["identity.md"]),
    SearchCase(
        "s02-belief",
        "Evidence and experimentation",
        "reversible experiment prediction",
        ["mind/beliefs.md"],
    ),
    SearchCase(
        "s03-values", "Entrusted information", "protect entrusted information", ["mind/values.md"]
    ),
    SearchCase("s04-person", "Sam's occupation", "Sam bookbinder diagrams", ["people/sam.md"]),
    SearchCase(
        "s05-decision-date",
        "Storage decision on May 1",
        "2026-05-01 Lantern SQLite storage decision",
        ["projects/lantern/2026/05-01.md"],
    ),
    SearchCase(
        "s06-project",
        "Inventory interchange format",
        "Lantern export CSV header",
        ["projects/lantern/2026/05-06.md"],
    ),
    SearchCase(
        "s07-repair", "Power source", "Kite replaceable battery", ["projects/kite/2026/05-05.md"]
    ),
    SearchCase(
        "s08-enclosure",
        "Enclosure fastening",
        "Kite enclosure captive screws",
        ["projects/kite/2026/05-09.md"],
    ),
    SearchCase(
        "s09-paraphrase",
        "Semantic retrieval without shared keywords",
        "Avoid interruptions while concentrating",
        ["notes/focus.md"],
    ),
    SearchCase(
        "s10-paraphrase",
        "Recovery procedure in different words",
        "Safeguard originals through verified backups",
        ["notes/recovery.md"],
    ),
    SearchCase(
        "s11-update",
        "The newest release day must rank first",
        "What is the current Lantern release day?",
        ["projects/lantern/2026/05-12.md"],
        ["projects/lantern/2026/05-03.md"],
        k=1,
    ),
    SearchCase(
        "s12-journal",
        "A physical prototype resolved a disagreement",
        "cardboard model awkward latch",
        ["journal/2026/05/week1/05-02.md"],
    ),
    SearchCase(
        "s13-digest",
        "An unresolved question in a daily digest",
        "glue dry open question",
        ["log/2026/05/10-digest.md"],
    ),
    SearchCase(
        "s14-no-answer",
        "There is no astronomy information in the fixture",
        "Neptune orbital eccentricity",
        [],
    ),
]
