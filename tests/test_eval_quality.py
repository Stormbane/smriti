"""H12.9: the versioned quality set over the synthetic corpus.

Retrieval by kind of question (hit@k, MRR), contradictions and corrections
(is the newer note found, and above the older?), and abstention (does an
irrelevant query's best result score lower than an answerable one's?).
The set's own invariants are held here; the scores with the real model are
held, loosely, by a needs_model test that the weekly job runs.
"""

from __future__ import annotations

import re

import pytest

from smriti.eval import quality
from smriti.eval.quality import (
    KINDS,
    QUALITY_CASES,
    QualityCase,
    QualityResult,
    auroc,
    run_quality_set,
    summarise,
)
from smriti.eval.runner import FIXTURE_ROOT, eval_sandbox
from tests._vec import needs_vec
from tests.test_eval_sandbox import LocalEmbedding


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def note(source: str) -> str:
    return (FIXTURE_ROOT / source).read_text(encoding="utf-8")


def note_date(source: str) -> str:
    """The date a note gives itself, or its digest's date; "" for an undated note."""
    found = re.search(r"2026-05-\d\d", note(source))
    if found:
        return found.group(0)
    digest = re.search(r"log/2026/05/(\d\d)-digest", source)
    return f"2026-05-{digest.group(1)}" if digest else ""


def test_the_set_is_well_formed() -> None:
    assert len({c.id for c in QUALITY_CASES}) == len(QUALITY_CASES)
    counts = {kind: sum(c.kind == kind for c in QUALITY_CASES) for kind in KINDS}
    assert counts["paraphrase"] >= 6 and counts["irrelevant"] >= 5
    assert counts["contradiction"] >= 3 and counts["correction"] >= 3
    for case in QUALITY_CASES:
        assert case.kind in KINDS
        for source in (*case.relevant, case.newer, case.older):
            if source:
                assert (FIXTURE_ROOT / source).is_file(), (case.id, source)
        assert bool(case.relevant) == (case.kind != "irrelevant"), case.id
        if case.kind in ("contradiction", "correction"):
            assert case.newer in case.relevant and case.newer != case.older, case.id
            assert note_date(case.newer) > note_date(case.older), case.id  # "" is oldest
            assert note_date(case.newer), case.id


def test_paraphrases_share_no_word_with_their_notes() -> None:
    for case in QUALITY_CASES:
        if case.kind == "paraphrase":
            for source in case.relevant:
                assert not words(case.query) & words(note(source)), (case.id, source)


def test_irrelevant_queries_share_no_word_with_the_corpus() -> None:
    corpus = set().union(*(words(p.read_text(encoding="utf-8"))
                           for p in FIXTURE_ROOT.rglob("*.md")))
    for case in QUALITY_CASES:
        if case.kind == "irrelevant":
            shared = {w for w in words(case.query) & corpus if len(w) > 2}
            assert not shared, (case.id, shared)


def test_a_correction_says_so_and_a_contradiction_does_not() -> None:
    for case in QUALITY_CASES:
        if case.kind == "correction":
            assert re.search(r"correction|supersedes", note(case.newer), re.IGNORECASE), case.id
        if case.kind == "contradiction":
            for source in (case.newer, case.older):
                assert not re.search(r"correction|supersedes|instead", note(source),
                                     re.IGNORECASE), (case.id, source)


def result(case: QualityCase, sources: list[str], score: float = 0.9,
           similarity: float | None = 0.6) -> QualityResult:
    return QualityResult(case, sources, score, similarity)


def test_the_measures() -> None:
    a = QualityCase("a", "direct", "q", ("x.md",))
    b = QualityCase("b", "paraphrase", "q", ("y.md", "z.md"))
    c = QualityCase("c", "correction", "q", ("new.md",), newer="new.md", older="old.md")
    d = QualityCase("d", "contradiction", "q", ("new.md", "old.md"),
                    newer="new.md", older="old.md")
    e = QualityCase("e", "irrelevant", "q")
    results = [
        result(a, ["x.md", "y.md"]),  # rank 1
        result(b, ["w.md", "v.md", "u.md", "z.md"]),  # rank 4
        result(c, ["old.md", "new.md"]),  # found, but under what it corrects
        result(d, ["old.md", "new.md"]),  # both found: resolved, not newer first
        result(e, ["x.md"], score=0.95, similarity=0.2),
    ]
    report = summarise("t", results)

    answerable = report.retrieval["answerable"]
    assert answerable["n"] == 4
    # First relevant ranks: a 1, b 4, c 2 (only the correction is relevant), d 1.
    assert answerable["hit@1"] == 2 / 4 and answerable["hit@3"] == 3 / 4
    assert answerable["hit@5"] == 1.0
    assert answerable["mrr"] == pytest.approx((1 + 1 / 4 + 1 / 2 + 1) / 4)
    assert report.disagreement["correction"] == {"n": 1, "resolved@5": 0.0, "newer_first": 0.0}
    assert report.disagreement["contradiction"] == {"n": 1, "resolved@5": 1.0,
                                                    "newer_first": 0.0}
    score = report.abstention["search_score"]
    assert score["abstained"] == 0.0 and score["false_abstention"] == 0.0
    assert score["auroc"] == 0.0  # the irrelevant query outscored every answerable one
    assert report.abstention["similarity"] == {"auroc": 1.0}


def test_no_similarity_row_without_vectors() -> None:
    a = QualityCase("a", "direct", "q", ("x.md",))
    e = QualityCase("e", "irrelevant", "q")
    report = summarise("t", [result(a, ["x.md"], similarity=None),
                             result(e, [], score=0.0, similarity=None)])
    assert "similarity" not in report.abstention
    assert report.abstention["search_score"]["abstained"] == 1.0


def test_auroc() -> None:
    assert auroc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert auroc([0.5], [0.5]) == 0.5
    assert auroc([], [0.1]) is None


@pytest.fixture()
def offline_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("smriti.embed.provider.embedder", lambda: LocalEmbedding())


@needs_vec
@pytest.mark.usefixtures("offline_embeddings")
def test_the_set_runs_end_to_end() -> None:
    with eval_sandbox() as root:
        report = run_quality_set(root)
    data = report.as_dict()
    assert data["version"] == quality.QUALITY_SET_VERSION
    assert data["cases"] == len(QUALITY_CASES) and not data["keyword_only"]
    assert data["embedder"]
    for row in data["retrieval"].values():
        assert all(0 <= row[k] <= 1 for k in ("hit@1", "hit@3", "hit@5", "mrr"))
    assert set(data["abstention"]) == {"search_score", "similarity"}
    # Word-hash vectors and the keyword leg find every direct question.
    assert data["retrieval"]["direct"]["hit@5"] == 1.0


@pytest.mark.usefixtures("offline_embeddings")
def test_smriti_eval_prints_the_set(capsys: pytest.CaptureFixture[str]) -> None:
    from smriti.cli import main

    assert main(["eval", "--markdown"]) == 0
    out = capsys.readouterr().out
    assert f"Quality set v{quality.QUALITY_SET_VERSION}, {len(QUALITY_CASES)} cases" in out
    assert "| correction |" in out and "| search score < 0.45 |" in out
    assert main(["eval"]) == 0
    assert "QUALITY SET v" in capsys.readouterr().out


@pytest.mark.needs_model
def test_with_the_real_model_the_scores_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loose floors under the scores bge-m3 (onnx, int8) gave on 2026-11-07:
    answerable hit@5 100%, MRR 0.86; paraphrase hit@3 100%; raw-similarity
    AUROC 0.99. A model that stopped working would fall far below them.
    And what its calibration (smriti.store.search.CALIBRATIONS) gives: two of
    four corrections above what they correct (one before it), four of six
    irrelevant queries scored under the threshold, no answerable one."""
    from tests._vec import can_load_vec

    if not can_load_vec():
        pytest.skip("sqlite-vec can't be loaded by this Python")
    with eval_sandbox() as root:
        report = run_quality_set(root)
    print(report.as_dict()["retrieval"], report.as_dict()["abstention"])
    assert not report.keyword_only
    assert report.retrieval["answerable"]["hit@5"] >= 0.85
    assert report.retrieval["answerable"]["mrr"] >= 0.7
    assert report.retrieval["paraphrase"]["hit@3"] >= 0.7
    similarity = report.abstention["similarity"]["auroc"]
    assert similarity is not None and similarity >= 0.85
    assert report.disagreement["correction"]["resolved@5"] >= 0.5
    score = report.abstention["search_score"]
    assert score["abstained"] is not None and score["abstained"] >= 0.5
    assert score["false_abstention"] == 0.0
