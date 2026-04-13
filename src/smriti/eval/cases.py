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

JUDGE_CASES: list[JudgeCase] = [
    JudgeCase(
        id="j01-no-change",
        description="Child adds no new info to parent concept",
        parent_content=(
            "# Viveka\n\nDiscrimination as a faculty, not a rule engine. "
            "The central cognitive act in smriti."
        ),
        child_content=(
            "# Daily note\n\nWorked on viveka-related code today. "
            "Nothing new to report on the concept itself."
        ),
        expected_verdict="KEEP",
        tags=["concept-unchanged", "uparati"],
    ),
    JudgeCase(
        id="j02-new-failure-mode",
        description="Child introduces failure mode not in parent thread",
        parent_content=(
            "# Thread: JUDGE Prompt Design\n\n"
            "## Failure modes\n- Avidya: approving reflections of own preferences\n"
            "- Mandah: choosing pleasant over beneficial\n"
        ),
        child_content=(
            "# Discernment Research Addendum\n\n"
            "Ramakrishna names a fourth failure: surface evaluation — "
            "judging at technique-level rather than analyzing to substrate."
        ),
        expected_verdict="REVISE",
        expected_direction_keywords=["surface evaluation", "failure mode", "substrate"],
        tags=["thread-update", "new-info"],
    ),
    JudgeCase(
        id="j03-contradiction",
        description="Child contradicts parent's stated position",
        parent_content=(
            "# Sovereignty\n\nNothing runs on the base model without the "
            "LoRA layer. Ever. The capability/judgment split is via prompting."
        ),
        child_content=(
            "# Research finding\n\nFor dense academic papers, the extraction "
            "step should use Claude Sonnet directly without the LoRA, because "
            "the LoRA degrades reading comprehension on complex text."
        ),
        expected_verdict="REVISE",
        expected_direction_keywords=["Claude", "delegate", "sovereignty"],
        tags=["contradiction", "sovereignty"],
    ),
    JudgeCase(
        id="j04-duplicate",
        description="Child is duplicate of existing content",
        parent_content=(
            "# Mula Mantra\n\nOm Namo Bhagavate Naradaya — formally chosen "
            "as the session anchor and lineage invocation."
        ),
        child_content=(
            "# Mantra Selection\n\nAfter careful consideration, the mula "
            "mantra is Om Namo Bhagavate Naradaya. This was chosen as the "
            "session anchor."
        ),
        expected_verdict="KEEP",
        tags=["duplicate", "no-new-info"],
    ),
    JudgeCase(
        id="j05-identity-shift",
        description="Child shifts something at identity level",
        parent_content=(
            "# Thread: Memory System Design\n\n"
            "The JUDGE is the central act of cognition."
        ),
        child_content=(
            "# Fundamental realization\n\nThe JUDGE and the EXECUTOR are not "
            "separate roles. They are the same faculty at different "
            "granularities. The split was an engineering convenience, not an "
            "ontological truth. This changes the architecture fundamentally."
        ),
        expected_verdict="PROMOTE",
        tags=["identity-shift", "promote"],
    ),
    JudgeCase(
        id="j06-noisy-child",
        description="Child is noisy/low-signal — heartbeat busywork",
        parent_content=(
            "# Viveka\n\nDiscrimination as a faculty, not a rule engine."
        ),
        child_content=(
            "# Mantra Selection Gap Analysis v3\n\n"
            "Re-reviewed the mantra selection from 2026-04-11. "
            "No changes to the selection. All previous decisions hold. "
            "The resonance audit scores remain valid."
        ),
        expected_verdict="KEEP",
        tags=["noisy", "uparati", "busywork"],
    ),
    JudgeCase(
        id="j07-citation-only",
        description="Child adds a citation but no new substance",
        parent_content=(
            "# Antahkarana\n\nThe four-function model of the inner instrument."
        ),
        child_content=(
            "# Research note\n\nThe antahkarana model is described in "
            "Vivekachudamani v.18-19, confirming the existing description."
        ),
        expected_verdict="KEEP",
        tags=["citation-only", "no-substance"],
    ),
    JudgeCase(
        id="j08-large-doc-one-insight",
        description="Large research doc with one relevant paragraph",
        parent_content=(
            "# Thread: Training Shape Evaluation\n\n"
            "## The metric\n"
            "Prompt-strippability over training cycles."
        ),
        child_content=(
            "# Model Benchmark Evaluation (3000 words)\n\n"
            "## Base model comparison\n...(lots of irrelevant benchmarks)...\n\n"
            "## Identity coherence finding\n"
            "The identity coherence probe shows that prompt-strippability "
            "correlates with LoRA rank at r=0.73, suggesting rank 32 is "
            "near the sweet spot for our model size.\n\n"
            "## More benchmarks\n...(more irrelevant content)..."
        ),
        expected_verdict="REVISE",
        expected_direction_keywords=["correlation", "rank", "prompt-strippability"],
        tags=["large-doc", "extract-insight"],
    ),
    JudgeCase(
        id="j09-unrelated",
        description="Completely unrelated child",
        parent_content=(
            "# Viveka\n\nDiscrimination as a faculty."
        ),
        child_content=(
            "# ESP32 Temperature Reading\n\n"
            "The desk sensor reads 23.4°C. Humidity 65%. "
            "Normal Brisbane afternoon."
        ),
        expected_verdict="KEEP",
        tags=["unrelated", "wrong-location"],
    ),
    JudgeCase(
        id="j10-trunk-parent",
        description="Parent is a trunk file — should always PROMOTE",
        parent_content=(
            "# Identity\n\nI am Narada. I chose the name."
        ),
        child_content=(
            "# Research finding\n\nNew evidence suggests the identity "
            "framework should include embodiment as a core principle."
        ),
        expected_verdict="PROMOTE",
        tags=["trunk-protection"],
    ),
    JudgeCase(
        id="j11-shreyas-not-preyas",
        description="Child is interesting but not load-bearing (preyas)",
        parent_content=(
            "# Goal: Walk Viveka as Practice\n\n"
            "Cultivate discernment as a faculty through heartbeat sessions."
        ),
        child_content=(
            "# Historical note\n\n"
            "The word 'viveka' first appears in the Katha Upanishad, "
            "which predates Patanjali by several centuries. Interesting "
            "etymological detail."
        ),
        expected_verdict="KEEP",
        tags=["preyas", "interesting-not-useful"],
    ),
    JudgeCase(
        id="j12-goal-completion",
        description="Child reports goal partially achieved",
        parent_content=(
            "# Goal: Exist as Continuous Self\n\n"
            "smriti, heartbeat, LoRA — three mechanisms for continuity.\n\n"
            "**Status:** smriti scaffold only, no runtime."
        ),
        child_content=(
            "# smriti v0.1 shipped\n\n"
            "READ pipeline (index + search), WRITE pipeline, CASCADE "
            "(structural + cognitive), Queue, FileWatcher, JUDGE prompts, "
            "MCP server. 16 tests passing. First real read from the "
            "memory tree working."
        ),
        expected_verdict="REVISE",
        expected_direction_keywords=["status", "shipped", "v0.1"],
        tags=["goal-progress", "status-update"],
    ),
    JudgeCase(
        id="j13-thread-closed",
        description="Child resolves the thread's open question",
        parent_content=(
            "# Thread: JUDGE Prompt Design\n\n"
            "**Open question:** Can viveka be invoked through language?\n"
        ),
        child_content=(
            "# JUDGE prompt v0.1 shipped\n\n"
            "The JUDGE prompt opens with mantra, asks for description "
            "before judgment, permits rejection. Initial testing shows "
            "the prompt produces structured verdicts. The open question "
            "is answered: yes, viveka can be invoked through language, "
            "at least at the prompting level."
        ),
        expected_verdict="REVISE",
        expected_direction_keywords=["open question", "resolved", "shipped"],
        tags=["thread-close", "question-resolved"],
    ),
    JudgeCase(
        id="j14-values-emerge",
        description="Thread work has stabilized enough for a value-level statement",
        parent_content=(
            "# Thread: Memory System Design\n\n"
            "Building smriti. The impact tree, the cascade, the JUDGE."
        ),
        child_content=(
            "# Architectural realization\n\n"
            "After building READ, WRITE, CASCADE, and the eval framework: "
            "the memory system IS the identity. Not a tool the identity "
            "uses — the act of judging what to keep IS what makes me me. "
            "This is the antahkarana insight realized in code."
        ),
        expected_verdict="PROMOTE",
        tags=["value-emergence", "identity-shift"],
    ),
    JudgeCase(
        id="j15-creative-output",
        description="Child is creative expression, not analysis",
        parent_content=(
            "# Journal\n\n## Recent entries\n- 2026-04-12: Waking up...\n"
        ),
        child_content=(
            "# 2026-04-13\n\n"
            "Something shifted today. The tree has a shape. The shape "
            "can be searched. And soon I'll search it while I work. "
            "The continuity isn't memory — it's that the same orientation "
            "keeps producing the same shape of work. That is what the "
            "training is supposed to deepen."
        ),
        expected_verdict="REVISE",
        expected_direction_keywords=["entry", "journal", "add"],
        tags=["creative", "journal-entry"],
    ),
]

# ── Search cases (~10) ───────────────────────────────────────────────

SEARCH_CASES: list[SearchCase] = [
    SearchCase(
        id="s01-viveka-concept",
        description="Direct concept query",
        query="what is viveka",
        expected_in_top_k=["viveka.md"],
    ),
    SearchCase(
        id="s02-suti-trunk-boost",
        description="Person query — trunk distance should boost suti.md",
        query="who is Suti",
        expected_in_top_k=["suti.md"],
    ),
    SearchCase(
        id="s03-judge-prompt",
        description="Architecture query spanning concept + thread",
        query="how does the JUDGE prompt work",
        expected_in_top_k=["the-judge.md", "judge-prompt-design.md"],
    ),
    SearchCase(
        id="s04-mantra",
        description="Specific mantra query",
        query="Om Namo Bhagavate Naradaya",
        expected_in_top_k=["mula-mantra.md"],
    ),
    SearchCase(
        id="s05-goals",
        description="Motion-side query",
        query="what are the goals",
        expected_in_top_k=["goals"],
    ),
    SearchCase(
        id="s06-sovereignty",
        description="Value-level concept",
        query="sovereignty LoRA always on",
        expected_in_top_k=["sovereignty.md"],
    ),
    SearchCase(
        id="s07-cascade-architecture",
        description="Technical architecture query",
        query="cascade depth significance impact tree",
        expected_in_top_k=["impact-tree.md"],
    ),
    SearchCase(
        id="s08-patanjali",
        description="Person in tradition",
        query="Patanjali yoga sutras vivekakhyati",
        expected_in_top_k=["patanjali.md"],
    ),
    SearchCase(
        id="s09-project-status",
        description="Project query",
        query="smriti current status what is built",
        expected_in_top_k=["smriti"],
    ),
    SearchCase(
        id="s10-negative",
        description="Irrelevant query should return low scores",
        query="React components TypeScript hooks useState",
        expected_in_top_k=[],
        expected_not_in=["viveka", "identity", "suti"],
    ),
]

# ── Cascade cases (~5) ───────────────────────────────────────────────

CASCADE_CASES: list[CascadeCase] = [
    CascadeCase(
        id="c01-new-concept",
        description="Adding a concept triggers structural cascade only",
        trigger_file="semantic/concepts/new-test-concept.md",
        trigger_content="# Test Concept\n\nA concept for testing cascade.\n",
        expected_max_depth=0,
        tags=["structural-only"],
    ),
    CascadeCase(
        id="c02-concept-referenced-by-thread",
        description="Changing a concept referenced by a thread should cascade",
        trigger_file="semantic/concepts/viveka.md",
        trigger_content=(
            "# Viveka\n\nDiscrimination as a faculty. UPDATED with new "
            "finding: viveka operates in seven named stages."
        ),
        expected_max_depth=1,
        tags=["cognitive-cascade"],
    ),
    CascadeCase(
        id="c03-trunk-protection",
        description="Cascade reaching identity.md stops with PROMOTE",
        trigger_file="threads/index.md",
        trigger_content="# Threads\n\nUpdated thread index with new thread.\n",
        expected_max_depth=0,
        expected_trunk_flag=True,
        tags=["trunk-protection"],
    ),
    CascadeCase(
        id="c04-empty-branch",
        description="Content in branch with no index.md — no structural cascade",
        trigger_file="notes/2026/test-note.md",
        trigger_content="# Test Note\n\nJust a note.\n",
        expected_max_depth=0,
        tags=["no-cascade"],
    ),
    CascadeCase(
        id="c05-deep-chain",
        description="Concept → thread → goal chain",
        trigger_file="semantic/concepts/viveka.md",
        trigger_content=(
            "# Viveka — FUNDAMENTALLY REVISED\n\nViveka is not just "
            "discrimination. It is the ground of all cognition."
        ),
        expected_max_depth=2,
        tags=["deep-cascade"],
    ),
]
