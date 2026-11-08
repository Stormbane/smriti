"""scripts/longmemeval.py: the history it writes, the questions it picks, what it scores.

A real run indexes hundreds of histories, far too slow for the suite; these
cover the parts that decide the numbers, and one marked run end to end over
a tiny invented dataset (the user Sam). The dataset's shape follows
LongMemEval's: a session id may begin "answer_", as its evidence sessions'
do.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "longmemeval.py"


@pytest.fixture(scope="module")
def lme() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smriti_longmemeval", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smriti_longmemeval"] = module  # dataclasses look their module up
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules["smriti_longmemeval"]
    return module


def _question(qid: str = "q1", qtype: str = "single-session-user") -> dict[str, Any]:
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": "Which city did I move to last spring?",
        "answer": "Lisbon",
        "question_date": "2023/06/01 (Thu) 10:00",
        "answer_session_ids": ["answer_abc123"],
        "haystack_session_ids": ["sharegpt_x1", "answer_abc123"],
        "haystack_dates": ["2023/05/20 (Sat) 02:21", "2023/05/21 (Sun) 18:05"],
        "haystack_sessions": [
            [{"role": "user", "content": "Can you suggest a sourdough schedule for weekends?"},
             {"role": "assistant", "content": "Sure.\nFeed the starter on Friday night."}],
            [{"role": "user", "content": "I moved to Lisbon last spring and love the trams.",
              "has_answer": True},
             {"role": "assistant", "content": "Lisbon's trams are lovely. Line 28 is famous."},
             {"role": "user", "content": "Thanks!", "has_answer": True}],
        ],
    }


# ----------------------------------------------------------------------------- the history

def test_dates_parse_with_or_without_a_time(lme: ModuleType) -> None:
    assert lme.date_parts("2023/05/20 (Sat) 02:21") == ("2023", "05", "20", "02:21")
    assert lme.date_parts("2023/05/20") == ("2023", "05", "20", "00:00")
    with pytest.raises(ValueError):
        lme.date_parts("May 20th")


def test_each_session_is_a_dated_render_with_an_opaque_name(lme: ModuleType) -> None:
    files = lme.session_files(_question())
    assert [f.rel for f in files] == ["conversations/2023/05/20-s000.md",
                                      "conversations/2023/05/21-s001.md"]
    assert [f.session_id for f in files] == ["sharegpt_x1", "answer_abc123"]
    first = files[0].text
    assert first.startswith("---\ndate: 2023-05-20\n---\n")
    assert "# Conversation, 2023-05-20 02:21" in first
    assert "- `02:21` **User:** Can you suggest a sourdough schedule" in first
    # A turn's later lines are indented, as the day-log renders them.
    assert "- `02:21` **Assistant:** Sure.\n  Feed the starter on Friday night." in first
    for f in files:  # a session id ("answer_...") would be a clue to search
        assert "answer" not in f.text.lower() and "sharegpt" not in f.text.lower()


def test_evidence_lines_come_from_evidence_turns_and_are_long_enough(lme: ModuleType) -> None:
    files = lme.session_files(_question())
    assert files[0].evidence_lines == ()
    # "Thanks!" is evidence too, but too short to say which turn a chunk holds.
    assert files[1].evidence_lines == ("I moved to Lisbon last spring and love the trams.",)


def test_smriti_dates_a_session_by_its_path(lme: ModuleType) -> None:
    from smriti.store.search import chunk_date

    days = [chunk_date(f.rel, "no date here") for f in lme.session_files(_question())]
    assert days == ["2023-05-20", "2023-05-21"]


def test_abstention_questions_are_named_by_their_id(lme: ModuleType) -> None:
    assert lme.is_abstention({"question_id": "gpt4_123_abs"})
    assert not lme.is_abstention({"question_id": "gpt4_123"})


# ----------------------------------------------------------------------------- selection

def _dataset() -> list[dict[str, Any]]:
    kinds = {"multi-session": 13, "temporal-reasoning": 13, "knowledge-update": 8,
             "single-session-user": 7, "single-session-assistant": 6,
             "single-session-preference": 3}
    return [{"question_id": f"{k}-{i}", "question_type": k}
            for k, n in kinds.items() for i in range(n)]


def test_a_sample_keeps_each_type_in_proportion(lme: ModuleType) -> None:
    data = _dataset()
    picked = lme.select(data, 10, seed=0)
    assert len(picked) == 10
    counts: dict[str, int] = {}
    for q in picked:
        counts[q["question_type"]] = counts.get(q["question_type"], 0) + 1
    for kind in {q["question_type"] for q in data}:
        share = sum(q["question_type"] == kind for q in data) * 10 / len(data)
        assert abs(counts.get(kind, 0) - share) < 1


def test_a_sample_is_the_same_for_a_seed_and_differs_across_seeds(lme: ModuleType) -> None:
    data = _dataset()
    ids = [q["question_id"] for q in lme.select(data, 10, seed=1)]
    assert ids == [q["question_id"] for q in lme.select(list(reversed(data)), 10, seed=1)]
    assert ids != [q["question_id"] for q in lme.select(data, 10, seed=2)]


def test_no_sample_or_a_large_one_takes_every_question(lme: ModuleType) -> None:
    data = _dataset()
    assert lme.select(data, None, seed=0) == data
    assert lme.select(data, 1000, seed=0) == data


# ----------------------------------------------------------------------------- measures

def test_sessions_are_ranked_where_they_first_appear(lme: ModuleType) -> None:
    by_source = {"a.md": "s1", "b/c.md": "s2"}
    assert lme.sessions_in_order(["b\\c.md", "a.md", "b/c.md", "gone.md"], by_source) == \
        ["s2", "s1"]


def test_a_chunk_shows_evidence_by_its_text_whatever_its_wrapping(lme: ModuleType) -> None:
    lines = ("I moved to Lisbon last spring and love the trams.",)
    assert lme.shows_evidence("- `18:05` **User:** I moved to Lisbon last\n  spring and love "
                              "the trams.", lines)
    assert not lme.shows_evidence("- `18:05` **User:** I moved to Porto.", lines)


def test_evidence_past_what_smriti_read_shows_is_not_seen(lme: ModuleType) -> None:
    line = "I moved to Lisbon last spring and love the trams."
    filler = "- `18:05` **Assistant:** " + "x" * (lme.MCP_PREVIEW_CHARS + 50) + "\n"
    late = filler + f"- `18:05` **User:** {line}"
    assert lme.shows_evidence(late, (line,))  # the whole chunk holds it
    assert not lme.shows_evidence(lme.visible(late), (line,))  # the agent never sees it


def test_an_evidence_line_cut_by_the_preview_counts_when_enough_shows(lme: ModuleType) -> None:
    line = "I moved to Lisbon last spring and love the trams."
    pad = "y" * (lme.MCP_PREVIEW_CHARS - 60)  # leaves 39 characters of the line shown
    cut = lme.visible(f"{pad}\n- `18:05` **User:** {line}")
    assert line not in cut and lme.shows_evidence(cut, (line,))
    too_little = lme.visible("z" * (lme.MCP_PREVIEW_CHARS - 5) + " I moved to Lisbon")
    assert not lme.shows_evidence(too_little, (line,))


def test_a_chunk_that_continues_a_long_evidence_line_shows_it(lme: ModuleType) -> None:
    # One long line: the chunker can split it, so a later chunk holds only its middle or end.
    line = lme.collapse("Some background on my week, which was busy with work and errands "
                        "and a few long calls with my family about the summer, and then "
                        "finally the news: I moved to Lisbon last spring and love the trams.")
    middle = "  calls with my family about the summer, and then finally the news: I moved to"
    end = "  finally the news: I moved to Lisbon last spring and love the trams.\n- `18:06` ..."
    assert lme.shows_evidence(middle, (line,))
    assert lme.shows_evidence(end, (line,))
    assert not lme.shows_evidence("the news: I moved to Porto in the winter, not Lisbon.",
                                  (line,))


def test_hybrid_needs_a_vector_for_every_chunk_and_no_fallback(lme: ModuleType) -> None:
    assert lme.hybrid_ok(10, 10, "")
    assert not lme.hybrid_ok(10, 0, "")  # vectors never stored (no sqlite-vec)
    assert not lme.hybrid_ok(10, 7, "")  # some pending
    assert not lme.hybrid_ok(10, 10, "sqlite-vec is not available")
    assert not lme.hybrid_ok(0, 0, "")  # nothing indexed at all


def test_a_run_with_a_degraded_search_is_incomplete(lme: ModuleType) -> None:
    ok = {"misses": 0, "hybrid_ok": True}
    assert lme.run_complete([0], [ok, ok], 2) == (True, "")
    done, why = lme.run_complete([0], [ok, {"misses": 0, "hybrid_ok": False}], 2)
    assert not done and "without vectors" in why
    assert not lme.run_complete([0], [ok, {"misses": 3, "hybrid_ok": True}], 2)[0]
    assert not lme.run_complete([1], [ok, ok], 2)[0]
    assert not lme.run_complete([0], [ok], 2)[0]


def _record(deep: list[str], agent: list[dict[str, Any]], evidence: list[str],
            *, abstention: bool = False, evidence_lines: bool = True,
            qtype: str = "multi-session", qid: str = "q") -> dict[str, Any]:
    mode = {"deep": deep, "agent": agent, "agent_ms": 1.0, "deep_ms": 1.0}
    return {"qid": qid, "type": qtype, "abstention": abstention, "evidence": evidence,
            "evidence_lines": evidence_lines, "modes": {"hybrid": mode, "keyword": mode}}


def test_the_measures_of_one_question(lme: ModuleType) -> None:
    deep = ["x1", "e1", "x2", "x3", "x4", "x5", "e2"]
    agent = [{"session": "x1", "evidence_turn": False},
             {"session": "e1", "evidence_turn": True}]
    s = lme.score(_record(deep, agent, ["e1", "e2"]), "hybrid")
    assert s["mrr"] == pytest.approx(0.5)
    assert s["recall_any@5"] == 1.0 and s["recall_all@5"] == 0.0
    assert s["recall_all@10"] == 1.0
    ideal = 1 + 1 / 1.5849625007211563
    got = 1 / 1.5849625007211563 + 1 / 3.0  # ranks 2 and 7: log2(3), log2(8)
    assert s["ndcg@10"] == pytest.approx(got / ideal)
    assert s["agent_session_hit"] == 1.0 and s["agent_turn_hit"] == 1.0


def test_a_question_whose_evidence_has_no_usable_line_has_no_turn_measure(
        lme: ModuleType) -> None:
    s = lme.score(_record(["e1"], [{"session": "e1", "evidence_turn": False}], ["e1"],
                          evidence_lines=False), "hybrid")
    assert s["agent_turn_hit"] is None and s["agent_session_hit"] == 1.0


def test_a_miss_scores_zero(lme: ModuleType) -> None:
    s = lme.score(_record(["x1", "x2"], [{"session": "x1", "evidence_turn": False}], ["e1"]),
                  "hybrid")
    assert s["mrr"] == 0.0 and s["recall_any@10"] == 0.0 and s["ndcg@10"] == 0.0
    assert s["agent_session_hit"] == 0.0


def test_the_summary_leaves_out_abstention_and_groups_by_type(lme: ModuleType) -> None:
    hit = [{"session": "e1", "evidence_turn": True}]
    records = [_record(["e1"], hit, ["e1"], qtype="temporal-reasoning", qid="a"),
               _record(["x1"], [], ["e1"], qtype="multi-session", qid="b"),
               _record(["e1"], hit, ["e1"], abstention=True, qid="c_abs"),
               _record(["x1"], [], [], qid="d")]  # names no evidence
    s = lme.summarise(records, "hybrid")
    assert s["all"]["n"] == 2 and s["all"]["recall_any@5"] == pytest.approx(0.5)
    assert s["temporal-reasoning"]["mrr"] == 1.0 and s["multi-session"]["mrr"] == 0.0


def test_agreement_between_runs(lme: ModuleType) -> None:
    a = [_record(["e1", "x1"], [], ["e1"], qid="a"), _record(["x2"], [], ["e1"], qid="b")]
    b = [_record(["e1", "x1"], [], ["e1"], qid="a"), _record(["e1"], [], ["e1"], qid="b")]
    agree = lme.agreement(a, b, "hybrid")
    assert agree["n"] == 2
    assert agree["top10_jaccard"] == pytest.approx(0.5)
    assert agree["same_recall_any@5"] == pytest.approx(0.5)


# ----------------------------------------------------------------------------- the vector cache

def test_the_cache_keeps_its_model_and_dimension(lme: ModuleType, tmp_path: Path) -> None:
    db = lme.open_cache(tmp_path / "v.db", 4, "m")
    assert lme.cache_dimension(db) == 4
    db.close()
    with pytest.raises(SystemExit):
        lme.open_cache(tmp_path / "v.db", 8, "m")
    with pytest.raises(SystemExit):
        lme.open_cache(tmp_path / "v.db", 4, "other")
    assert lme.unpack(lme.pack([0.5, -1.0, 2.0])) == [0.5, -1.0, 2.0]


def test_the_onnx_provider_reads_the_cache_and_lists_what_it_lacks(
        lme: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti._vendored.memsearch.embeddings import onnx as onnx_module

    # Registered first, so the original comes back however the test ends.
    monkeypatch.setattr(onnx_module.OnnxEmbedding, "_encode",
                        onnx_module.OnnxEmbedding._encode)
    cache = tmp_path / "v.db"
    db = lme.open_cache(cache, 3, "m")
    db.execute("INSERT INTO vectors VALUES (?, ?)",
               (lme.text_key("known"), lme.pack([0.0, 1.0, 0.0])))
    db.commit()
    db.close()
    misses = tmp_path / "misses.jsonl"
    missed = lme.use_vector_cache(cache, misses)
    provider = object.__new__(onnx_module.OnnxEmbedding)  # no model loaded
    got = provider._encode(["known", "unknown", "unknown"])
    assert got == [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
    assert missed == [2]
    listed = [json.loads(line) for line in misses.read_text(encoding="utf-8").splitlines()]
    assert listed == [{"k": lme.text_key("unknown"), "text": "unknown"}]


# ----------------------------------------------------------------------------- run and report

def test_the_worker_runs_the_checkouts_code_without_a_root_or_service(
        lme: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMRITI_ROOT", "/somewhere/real")
    monkeypatch.setenv("NARADA_ROOT", "/somewhere/real")
    checkout = tmp_path / "co"
    (checkout / "src").mkdir(parents=True)
    env = lme.worker_env(checkout, tmp_path / "run")
    assert "SMRITI_ROOT" not in env and "NARADA_ROOT" not in env
    assert env["PYTHONPATH"] == str(checkout / "src")
    assert env["SMRITI_EMBED_SERVICE"] == "0" and env["SMRITI_EMBED_PROVIDER"] == "onnx"
    assert env["SMRITI_RUNTIME_DIR"] == str(tmp_path / "run" / "runtime")


def test_report_refuses_an_incomplete_run(lme: ModuleType, tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "meta.json").write_text(json.dumps(
        {"label": "x", "complete": False, "misses": 3, "recorded": 1, "questions": 2}))
    with pytest.raises(SystemExit, match="incomplete"):
        lme.main(["report", str(run)])


def _complete_run(path: Path, label: str, records: list[dict[str, Any]]) -> Path:
    path.mkdir(parents=True)
    (path / "meta.json").write_text(json.dumps(
        {"label": label, "complete": True, "commit": "c0ffee", "vectors": None}))
    (path / "results-0.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_report_keeps_runs_apart_when_their_labels_match(
        lme: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hit = [{"session": "e1", "evidence_turn": True}]
    a = _complete_run(tmp_path / "x-1", "x", [_record(["e1"], hit, ["e1"], qid="q")])
    b = _complete_run(tmp_path / "x-2", "x", [_record(["z9"], [], ["e1"], qid="q")])
    out = tmp_path / "report.json"
    assert lme.main(["report", str(a), str(b), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "| x (x-1) | hybrid | 1 | 1.000" in text
    assert "| x (x-2) | hybrid | 1 | 0.000" in text
    data = json.loads(out.read_text())
    assert data["x-1"]["hybrid"]["all"]["mrr"] == 1.0
    assert data["x-2"]["hybrid"]["all"]["mrr"] == 0.0


@pytest.mark.needs_model
def test_a_run_end_to_end_with_the_real_model(tmp_path: Path) -> None:
    pytest.importorskip("onnxruntime")
    data = tmp_path / "lme.json"
    data.write_text(json.dumps([_question("q1"), _question("q2_abs", "knowledge-update")]),
                    encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "SMRITI_EMBED_PROVIDER"}
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "run", str(SCRIPT.parents[1]), "--data", str(data),
         "--label", "t", "--work", str(tmp_path / "runs")],
        capture_output=True, text=True, env=env, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    run = next((tmp_path / "runs").iterdir())
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    assert meta["complete"] and meta["recorded"] == 2
    assert not (run / "roots" / "q1").exists()  # deleted once scored
    records = {r["qid"]: r for r in (
        json.loads(line) for line in (run / "results-0.jsonl").read_text().splitlines())}
    q1 = records["q1"]
    assert q1["vector_rows"] > 0
    # Two sessions: the one about Lisbon answers a question about moving city.
    assert q1["modes"]["hybrid"]["deep"][0] == "answer_abc123"
    report = subprocess.run([sys.executable, str(SCRIPT), "report", str(run)],
                            capture_output=True, text=True, env=env, timeout=120)
    assert report.returncode == 0 and "| t | hybrid | 1 |" in report.stdout
