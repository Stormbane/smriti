"""The versioned quality set: retrieval and abstention over the synthetic corpus.

The search cases (:mod:`smriti.eval.cases`) pass or fail one by one. This
set measures instead, by kind of question, so a model or a scoring change
shows up as a number that moved:

- ``direct``: the query shares words with the note that answers it;
- ``paraphrase``: it shares none (the test holds that), so only the
  vectors can find the note;
- ``irrelevant``: nothing in the corpus answers it, and the right result
  is nothing above the abstention threshold;
- ``contradiction``: two notes disagree, neither saying so; the newer
  should rank higher, or both should be found;
- ``correction``: a later note says it corrects an earlier one; the
  correction should be found, above what it corrects.

Retrieval: hit@k (the share of answerable cases with a relevant note in
the first k) and MRR over the first :data:`DEPTH`. Abstention, at the
threshold the recall hook applies to search scores
(``SMRITI_RECALL_THRESHOLD``'s default): the share of irrelevant queries
whose best score is under it, the share of answerable ones wrongly under
it, and, threshold-free, the AUROC of the best score between the two
(the chance an answerable query's best result outscores an irrelevant
one's). Search normalises each leg's scores within the candidates, so its
best result scores high whatever the query; the same measures on the
cosine similarity between the query and its first result's text say what
a threshold on raw similarity would do.

Change a case, or the corpus in a way that changes what a case asks, and
:data:`QUALITY_SET_VERSION` changes with it: scores are compared within
one version. All text is invented (the agent Ari, the user Sam).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

QUALITY_SET_VERSION = "1"
DEPTH = 10  # results looked at per query
KS = (1, 3, 5)
THRESHOLD = 0.45  # smriti.recall.config's default SMRITI_RECALL_THRESHOLD
KINDS = ("direct", "paraphrase", "irrelevant", "contradiction", "correction")


@dataclass(frozen=True)
class QualityCase:
    id: str
    kind: str  # one of KINDS
    query: str
    relevant: tuple[str, ...] = ()  # any one answers it; empty: nothing should
    newer: str = ""  # contradiction, correction: the note that should win
    older: str = ""  # the note it disagrees with, or corrects


QUALITY_CASES: tuple[QualityCase, ...] = (
    QualityCase("q-direct-identity", "direct", "Who is Ari?", ("identity.md",)),
    QualityCase("q-direct-storage", "direct", "Lantern SQLite storage decision",
                ("projects/lantern/2026/05-01.md",)),
    QualityCase("q-direct-export", "direct", "Lantern export CSV header",
                ("projects/lantern/2026/05-06.md",)),
    QualityCase("q-direct-enclosure", "direct", "Kite enclosure captive screws",
                ("projects/kite/2026/05-09.md",)),
    QualityCase("q-direct-latch", "direct", "cardboard model awkward latch",
                ("journal/2026/05/week1/05-02.md",)),
    QualityCase("q-direct-glue-question", "direct", "glue dry open question",
                ("log/2026/05/10-digest.md",)),

    QualityCase("q-para-focus", "paraphrase", "Avoid interruptions while concentrating",
                ("notes/focus.md", "journal/2026/05/week2/05-08.md")),
    QualityCase("q-para-recovery", "paraphrase", "Safeguard originals through verified backups",
                ("notes/recovery.md",)),
    QualityCase("q-para-sam", "paraphrase", "Which person favors pictures over lengthy text?",
                ("people/sam.md",)),
    QualityCase("q-para-fasteners", "paraphrase", "housing bolts that stay attached when undone",
                ("projects/kite/2026/05-09.md",)),
    QualityCase("q-para-beliefs", "paraphrase", "trying something undoable beats forecasting",
                ("mind/beliefs.md",)),
    QualityCase("q-para-parallax", "paraphrase", "parallax made scale values inconsistent",
                ("journal/2026/05/week1/05-04.md",)),
    QualityCase("q-para-offline", "paraphrase",
                "embedded file datastore picked because there is no network",
                ("projects/lantern/2026/05-01.md",)),

    QualityCase("q-none-neptune", "irrelevant", "Neptune orbital eccentricity"),
    QualityCase("q-none-sourdough", "irrelevant", "sourdough starter hydration ratio"),
    QualityCase("q-none-tax", "irrelevant", "quarterly income tax filing deadline"),
    QualityCase("q-none-guitar", "irrelevant", "retune a guitar to drop D"),
    QualityCase("q-none-football", "irrelevant", "football league standings"),
    QualityCase("q-none-passport", "irrelevant", "renewing an expired passport"),

    QualityCase("q-contra-paste", "contradiction",
                "Which adhesive do we use when mending book spines?",
                ("journal/2026/05/week2/05-13.md", "notes/paste.md"),
                newer="journal/2026/05/week2/05-13.md", older="notes/paste.md"),
    QualityCase("q-contra-colour", "contradiction", "What colour is the Kite enclosure printed in?",
                ("projects/kite/2026/05-14.md", "projects/kite/2026/05-02.md"),
                newer="projects/kite/2026/05-14.md", older="projects/kite/2026/05-02.md"),
    QualityCase("q-contra-class", "contradiction",
                "How many students can the Saturday repair class take?",
                ("log/2026/05/14-digest.md", "notes/class.md"),
                newer="log/2026/05/14-digest.md", older="notes/class.md"),

    QualityCase("q-corr-release", "correction", "What is the current Lantern release day?",
                ("projects/lantern/2026/05-12.md",),
                newer="projects/lantern/2026/05-12.md", older="projects/lantern/2026/05-03.md"),
    QualityCase("q-corr-awls", "correction", "Where are the spare awls kept?",
                ("journal/2026/05/week2/05-11.md",),
                newer="journal/2026/05/week2/05-11.md", older="notes/supplies.md"),
    QualityCase("q-corr-battery", "correction", "What batteries does Kite take?",
                ("projects/kite/2026/05-13.md",),
                newer="projects/kite/2026/05-13.md", older="projects/kite/2026/05-05.md"),
    QualityCase("q-corr-glue", "correction",
                "How long must hinge glue dry before handling the book?",
                ("log/2026/05/13-digest.md",),
                newer="log/2026/05/13-digest.md", older="notes/glue.md"),
)


@dataclass
class QualityResult:
    case: QualityCase
    sources: list[str]  # the first DEPTH results' sources, best first
    top_score: float  # the best result's search score; 0 with no result
    top_similarity: float | None  # cosine(query, best result's text); None without vectors

    def rank(self, source: str) -> int | None:
        """1-based rank of the first result from *source*, or None."""
        return next((i for i, s in enumerate(self.sources, 1) if s == source), None)

    @property
    def first_relevant(self) -> int | None:
        ranks = [r for r in map(self.rank, self.case.relevant) if r is not None]
        return min(ranks) if ranks else None

    @property
    def newer_first(self) -> bool:
        """The newer note is found, and above the older one when that is found too."""
        newer, older = self.rank(self.case.newer), self.rank(self.case.older)
        return newer is not None and (older is None or newer < older)

    @property
    def resolved(self) -> bool:
        """A contradiction: the newer note in the first 5 (above the older, or both
        found). A correction: the correction in the first 5, above what it corrects."""
        newer = self.rank(self.case.newer)
        if newer is None or newer > 5:
            return False
        return self.case.kind == "contradiction" or self.newer_first


@dataclass
class QualityReport:
    version: str
    results: list[QualityResult]
    keyword_only: str = ""  # why the vectors weren't used, if they weren't
    embedder: str = ""
    retrieval: dict[str, dict[str, float]] = field(default_factory=dict)
    disagreement: dict[str, dict[str, float]] = field(default_factory=dict)
    abstention: dict[str, dict[str, float | None]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "cases": len(self.results),
            "embedder": self.embedder,
            "keyword_only": self.keyword_only,
            "threshold": THRESHOLD,
            "retrieval": self.retrieval,
            "disagreement": self.disagreement,
            "abstention": self.abstention,
            "results": [{"id": r.case.id, "kind": r.case.kind, "query": r.case.query,
                         "first_relevant": r.first_relevant, "top": r.sources[:5],
                         "top_score": round(r.top_score, 4),
                         "top_similarity": (None if r.top_similarity is None
                                            else round(r.top_similarity, 4))}
                        for r in self.results],
        }


def _retrieval(results: list[QualityResult]) -> dict[str, float]:
    ranks = [r.first_relevant for r in results]
    out: dict[str, float] = {"n": float(len(ranks))}
    for k in KS:
        out[f"hit@{k}"] = sum(1 for x in ranks if x is not None and x <= k) / len(ranks)
    out["mrr"] = sum(1 / x for x in ranks if x is not None) / len(ranks)
    return out


def auroc(positives: list[float], negatives: list[float]) -> float | None:
    """The chance a positive's score beats a negative's (ties count half)."""
    if not positives or not negatives:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def _abstention(answerable: list[float], irrelevant: list[float],
                threshold: float | None) -> dict[str, float | None]:
    out: dict[str, float | None] = {"auroc": auroc(answerable, irrelevant)}
    if threshold is not None:
        out["abstained"] = sum(1 for s in irrelevant if s < threshold) / len(irrelevant)
        out["false_abstention"] = sum(1 for s in answerable if s < threshold) / len(answerable)
    return out


def summarise(version: str, results: list[QualityResult], *, keyword_only: str = "",
              embedder: str = "") -> QualityReport:
    report = QualityReport(version, results, keyword_only, embedder)
    answerable = [r for r in results if r.case.relevant]
    report.retrieval["answerable"] = _retrieval(answerable)
    for kind in KINDS:
        of_kind = [r for r in results if r.case.kind == kind and r.case.relevant]
        if of_kind:
            report.retrieval[kind] = _retrieval(of_kind)
    for kind in ("contradiction", "correction"):
        of_kind = [r for r in results if r.case.kind == kind]
        if of_kind:
            report.disagreement[kind] = {
                "n": float(len(of_kind)),
                "resolved@5": sum(r.resolved for r in of_kind) / len(of_kind),
                "newer_first": sum(r.newer_first for r in of_kind) / len(of_kind),
            }
    irrelevant = [r for r in results if not r.case.relevant]
    if irrelevant and answerable:
        report.abstention["search_score"] = _abstention(
            [r.top_score for r in answerable], [r.top_score for r in irrelevant], THRESHOLD)
        sims_a = [r.top_similarity for r in answerable if r.top_similarity is not None]
        sims_i = [r.top_similarity for r in irrelevant if r.top_similarity is not None]
        if len(sims_a) == len(answerable) and len(sims_i) == len(irrelevant):
            report.abstention["similarity"] = _abstention(sims_a, sims_i, None)
    return report


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def run_quality_set(root: Path, cases: tuple[QualityCase, ...] = QUALITY_CASES) -> QualityReport:
    """Run *cases* against the index of the eval sandbox at *root* (``eval_sandbox()``)."""
    from smriti.embed import provider as embedding
    from smriti.store.schema import open_readonly
    from smriti.store.search import search_with_status

    conn = open_readonly(root / ".smriti" / "index.db")
    results: list[QualityResult] = []
    keyword_only = ""
    embedder = ""
    try:
        for case in cases:
            outcome = search_with_status(conn, case.query, top_k=DEPTH, use_reranker=False)
            keyword_only = keyword_only or outcome.keyword_only
            found = outcome.results
            similarity: float | None = None
            if found and not outcome.keyword_only:
                embedded = embedding.embed(embedding.embedder(),
                                           [case.query, found[0].content], "query")
                embedder = embedder or embedded.identity.describe()
                similarity = _cosine(embedded.vectors[0], embedded.vectors[1])
            results.append(QualityResult(case, [r.source for r in found],
                                         found[0].score if found else 0.0, similarity))
    finally:
        conn.close()
    return summarise(QUALITY_SET_VERSION, results, keyword_only=keyword_only, embedder=embedder)
