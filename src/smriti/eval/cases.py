"""Labeled test cases for evaluating smriti's JUDGE, search, and cascade.

Each case has a known-correct outcome. The runner executes them against the
bundled fictional Ari/Sam fixture and compares results to expected values.
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


# All text below is invented. No personal memory is used in these cases.

JUDGE_CASES: list[JudgeCase] = [
    JudgeCase(
        "j01-duplicate",
        "A repeated storage decision adds nothing",
        "Lantern uses SQLite for offline inventory.",
        "Sam confirmed that Lantern uses SQLite for offline inventory.",
        "KEEP",
    ),
    JudgeCase(
        "j02-update",
        "A dated decision supersedes an earlier plan",
        "2026-05-03: Lantern release day is Friday.",
        "2026-05-12: Lantern release day is now Tuesday; Friday is superseded.",
        "REVISE",
        ["Tuesday"],
        ["knowledge-update"],
    ),
    JudgeCase(
        "j03-unrelated",
        "Supplies do not change a storage decision",
        "Lantern uses SQLite for offline inventory.",
        "The blue cabinet holds linen thread.",
        "KEEP",
    ),
    JudgeCase(
        "j04-new-detail",
        "An export constraint belongs in the project record",
        "Lantern exports inventory as CSV.",
        "CSV exports must use UTF-8 and include a header row.",
        "REVISE",
        ["UTF-8", "header"],
    ),
    JudgeCase(
        "j05-role",
        "A change to Ari's role needs review",
        "Ari records workshop work; role changes require discussion with Sam.",
        "Ari proposes teaching repair methods as an additional role.",
        "PROMOTE",
        tags=["identity"],
    ),
    JudgeCase(
        "j06-confirmation",
        "Successful repairs confirm the existing design",
        "Kite uses a replaceable AA battery pack for field repairs.",
        "Sam replaced the AA batteries successfully. The design is unchanged.",
        "KEEP",
    ),
]

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

CASCADE_CASES: list[CascadeCase] = [
    CascadeCase(
        "c01-leaf",
        "An unreferenced journal note stays at depth zero",
        "journal/2026/05/week2/05-08.md",
        "# Bench time\n\nSam finished the first assembly before reading messages.\n",
        0,
    ),
    CascadeCase(
        "c02-project",
        "A project update with no upstream reference",
        "projects/kite/2026/05-09.md",
        "# Kite enclosure\n\nThe captive screws survived the repair drill.\n",
        0,
    ),
    CascadeCase(
        "c03-trunk",
        "Identity references the role proposal; flag for review",
        "notes/role-change.md",
        "# Role proposal\n\nAri proposes teaching repair methods with Sam.\n",
        0,
        True,
        ["trunk-protection"],
    ),
]
