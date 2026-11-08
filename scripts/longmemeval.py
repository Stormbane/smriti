"""LongMemEval retrieval for smriti checkouts: does search find the session that holds the answer?

    python scripts/longmemeval.py run CHECKOUT --data FILE [--label NAME] [--work DIR]
                                  [--questions N] [--seed S] [--jobs N] [--vectors CACHE] [--keep]
    python scripts/longmemeval.py embed --vectors CACHE --misses FILE [FILE ...]
                                        --model DIR [--device cuda]
    python scripts/longmemeval.py report RUN [RUN ...] [--json FILE]

LongMemEval (Wu et al., ICLR 2025, MIT licence) asks 500 questions, each over its
own history of about 50 chat sessions. This reads its cleaned release,
``longmemeval_s_cleaned.json`` from
https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned; download it
yourself. Nothing here fetches it, and the repository carries none of it.

``run`` measures one checkout's search as an agent's ``smriti_read`` gets it.
For each question it writes the history into a fresh memory root, one dated
markdown file per session in the day-log's render format, under
``conversations/YYYY/MM/DD-sNNN.md`` (the name carries no session id, which
in the dataset can say "answer"); indexes the root with that checkout's
``index_tree``; and asks that checkout's ``search`` the question. A worker
does this with ``PYTHONPATH=<checkout>/src``, as ``scripts/bench.py`` runs
each side, so an older checkout runs its own code. Per question it records,
with vectors and keyword-only:

- the agent's view: the chunks ``smriti_read`` returns by default
  (:data:`AGENT_TOP_K`, search's default candidates, no reranker), each with
  its session and whether it shows an evidence turn;
- a deeper ranking (:data:`DEEP_TOP_K` chunks), reduced to sessions in the
  order they first appear.

``report`` gives the measures of LongMemEval's retrieval evaluation over the
answerable questions (an abstention question names no evidence): session
recall_any@k and recall_all@k, NDCG@10 and MRR on the deeper ranking; and
for the agent's view, whether a chunk comes from an evidence session and
whether one shows an evidence turn (holds a line of one, by text). Given
runs over the same questions, it also says how often their rankings agree.

What this measures is search over raw conversations. A smriti install
doesn't index its raw day-log renders (``store.index_policy``): it searches
the nightly's digests and what the agent wrote. So this is the search
engine's quality on the history it would be given, not the recall of an
install as configured.

Embeddings. smriti's default model, bge-m3 int8 under onnxruntime, embeds
about one chunk a second on a CPU, and a full run has about 150,000 chunks.
With ``--vectors CACHE`` the checkout's onnx provider takes each text's
vector from a SQLite cache instead of computing it. A text missing from the
cache gets a placeholder vector and is listed in ``<run>/misses.jsonl``,
and the run is marked incomplete, which ``report`` refuses. ``embed`` fills
the cache from those lists with an ONNX export of the model, encoded as the
provider encodes (for bge-m3, the full-precision export in BAAI/bge-m3's
``onnx/``, on a GPU under onnxruntime-gpu; the int8 export's quantised
operators fall back to the CPU there), and the run is repeated. Its vectors
stand in for the int8 model's, which the index still names, so the int8
model's calibration applies to them; run some questions without
``--vectors`` as well and pass both runs to ``report`` to see how far the
two agree. Without ``--vectors`` the checkout's own provider computes every
vector.

Each run goes to a new directory under ``--work``. Its roots are deleted as
each question is scored unless ``--keep``; nothing else is deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AGENT_TOP_K = 5  # smriti_read's default top_k
AGENT_CANDIDATE_K = 20  # search's default candidate_k
DEEP_TOP_K = 50
DEEP_CANDIDATE_K = 50
KS = (5, 10)
NDCG_K = 10
# A line of an evidence turn shorter than this is too common ("Thanks!") to
# say which turn a chunk holds.
EVIDENCE_LINE_MIN = 20
DEFAULT_DIMENSION = 1024  # bge-m3's, for a cache that has no vectors yet
MODES = (("hybrid", True), ("keyword", False))
MEASURES = ("recall_any@5", "recall_all@5", "recall_any@10", "recall_all@10",
            f"ndcg@{NDCG_K}", "mrr", "agent_session_hit", "agent_turn_hit")

_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*(?:\(\w+\))?\s*(\d{2}:\d{2})?")


# ----------------------------------------------------------------------------- the history

@dataclass(frozen=True)
class SessionFile:
    rel: str  # tree-relative, "/"-separated
    session_id: str
    text: str
    evidence_lines: tuple[str, ...]  # collapsed lines of its evidence turns


def collapse(text: str) -> str:
    return " ".join(text.split())


def date_parts(date: str) -> tuple[str, str, str, str]:
    """``"2023/05/20 (Sat) 02:21"`` → ``("2023", "05", "20", "02:21")``."""
    m = _DATE.match(date.strip())
    if not m:
        raise ValueError(f"unrecognised LongMemEval date {date!r}")
    return m.group(1), m.group(2), m.group(3), m.group(4) or "00:00"


def session_files(question: dict[str, Any]) -> list[SessionFile]:
    """One file per haystack session, as the day-log renders a conversation."""
    files: list[SessionFile] = []
    for i, (sid, date, turns) in enumerate(zip(
            question["haystack_session_ids"], question["haystack_dates"],
            question["haystack_sessions"], strict=True)):
        y, m, d, clock = date_parts(date)
        lines = ["---", f"date: {y}-{m}-{d}", "---", "",
                 f"# Conversation, {y}-{m}-{d} {clock}", ""]
        evidence: list[str] = []
        for turn in turns:
            speaker = "User" if turn["role"] == "user" else "Assistant"
            text = str(turn["content"]).strip().replace("\r\n", "\n").replace("\r", "\n")
            lines.append(f"- `{clock}` **{speaker}:** " + text.replace("\n", "\n  "))
            if turn.get("has_answer"):
                evidence += [c for line in text.split("\n")
                             if len(c := collapse(line)) >= EVIDENCE_LINE_MIN]
        files.append(SessionFile(f"conversations/{y}/{m}/{d}-s{i:03d}.md", sid,
                                 "\n".join(lines) + "\n", tuple(evidence)))
    return files


def is_abstention(question: dict[str, Any]) -> bool:
    return str(question["question_id"]).endswith("_abs")


def select(data: Sequence[dict[str, Any]], n: int | None, seed: int) -> list[dict[str, Any]]:
    """All questions, or *n* of them in proportion to each question type (seeded)."""
    if n is None or n >= len(data):
        return list(data)
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for q in data:
        by_type[q["question_type"]].append(q)
    rng = random.Random(seed)
    for t in sorted(by_type):  # in a fixed order, so the input's order doesn't matter
        by_type[t].sort(key=lambda q: q["question_id"])
        rng.shuffle(by_type[t])
    quotas = {t: n * len(qs) / len(data) for t, qs in by_type.items()}
    take = {t: int(v) for t, v in quotas.items()}
    for t in sorted(quotas, key=lambda t: (take[t] - quotas[t], t))[:n - sum(take.values())]:
        take[t] += 1
    return [q for t in sorted(by_type) for q in by_type[t][:take[t]]]


# ----------------------------------------------------------------------------- measures

def sessions_in_order(sources: Iterable[str], by_source: dict[str, str]) -> list[str]:
    """The sessions of ranked chunks, each once, where it first appears."""
    seen: list[str] = []
    for source in sources:
        sid = by_source.get(source.replace("\\", "/"))
        if sid is not None and sid not in seen:
            seen.append(sid)
    return seen


def shows_evidence(content: str, evidence_lines: Sequence[str]) -> bool:
    text = collapse(content)
    return any(line in text for line in evidence_lines)


def score(record: dict[str, Any], mode: str) -> dict[str, float | None]:
    """One answerable question's measures in one mode (see the module docstring)."""
    evidence = set(record["evidence"])
    got = record["modes"][mode]
    ranked: list[str] = got["deep"]
    first = next((i for i, s in enumerate(ranked, 1) if s in evidence), None)
    out: dict[str, float | None] = {"mrr": 1.0 / first if first else 0.0}
    for k in KS:
        top = set(ranked[:k])
        out[f"recall_any@{k}"] = float(bool(top & evidence))
        out[f"recall_all@{k}"] = float(evidence <= top)
    dcg = sum(1 / math.log2(i + 2) for i, s in enumerate(ranked[:NDCG_K]) if s in evidence)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(evidence), NDCG_K)))
    out[f"ndcg@{NDCG_K}"] = dcg / ideal if ideal else 0.0
    agent = got["agent"]
    out["agent_session_hit"] = float(any(c["session"] in evidence for c in agent))
    out["agent_turn_hit"] = (float(any(c["evidence_turn"] for c in agent))
                             if record["evidence_lines"] else None)
    return out


def summarise(records: Sequence[dict[str, Any]], mode: str) -> dict[str, dict[str, Any]]:
    """Mean measures over the answerable questions: overall and by question type."""
    groups: dict[str, list[dict[str, float | None]]] = defaultdict(list)
    for r in records:
        if r["abstention"] or not r["evidence"]:
            continue
        s = score(r, mode)
        groups["all"].append(s)
        groups[r["type"]].append(s)
    out: dict[str, dict[str, Any]] = {}
    for group, scores in groups.items():
        row: dict[str, Any] = {"n": len(scores)}
        for m in MEASURES:
            vals = [v for s in scores if (v := s[m]) is not None]
            row[m] = sum(vals) / len(vals) if vals else None
        out[group] = row
    return out


def agreement(a: Sequence[dict[str, Any]], b: Sequence[dict[str, Any]],
              mode: str) -> dict[str, float | int]:
    """How far two runs' rankings agree over the questions both answered."""
    bq = {r["qid"]: r for r in b}
    pairs = [(r, bq[r["qid"]]) for r in a if r["qid"] in bq and not r["abstention"]]
    if not pairs:
        return {"n": 0}
    jac, same5 = [], 0
    for ra, rb in pairs:
        ta, tb = set(ra["modes"][mode]["deep"][:10]), set(rb["modes"][mode]["deep"][:10])
        jac.append(len(ta & tb) / len(ta | tb) if ta | tb else 1.0)
        same5 += score(ra, mode)["recall_any@5"] == score(rb, mode)["recall_any@5"]
    return {"n": len(pairs), "top10_jaccard": sum(jac) / len(jac),
            "same_recall_any@5": same5 / len(pairs)}


# ----------------------------------------------------------------------------- the vector cache

def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def open_cache(path: Path, dimension: int | None = None, model: str | None = None,
               ) -> sqlite3.Connection:
    """The cache, created if needed: ``meta`` (dimension, model) and ``vectors``.

    In WAL mode, with a long busy wait: workers read it while ``embed``
    writes, and a reader that gave up would end its run.
    """
    db = sqlite3.connect(str(path), check_same_thread=False, timeout=600.0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS vectors (k TEXT PRIMARY KEY, v BLOB NOT NULL)")
    meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
    for key, value in (("dimension", dimension), ("model", model)):
        if value is None:
            continue
        if key in meta and meta[key] != str(value):
            db.close()
            raise SystemExit(f"{path} holds {key} {meta[key]}, not {value}")
        db.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (key, str(value)))
    db.commit()
    return db


def cache_dimension(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT v FROM meta WHERE k = 'dimension'").fetchone()
    return int(row[0]) if row else DEFAULT_DIMENSION


def pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def use_vector_cache(cache: Path, misses: Path) -> list[int]:
    """Make the onnx provider read vectors from *cache*; returns a one-item miss counter.

    Both the current and older checkouts embed through
    ``OnnxEmbedding._encode`` in the vendored memsearch, so patching it there
    covers indexing and the query alike.
    """
    from smriti._vendored.memsearch.embeddings import onnx as onnx_module

    db = open_cache(cache)
    dimension = cache_dimension(db)
    placeholder = [1.0] + [0.0] * (dimension - 1)
    missed = [0]
    listed: set[str] = set()
    lock = threading.Lock()  # _encode runs in the provider's executor threads
    out = misses.open("a", encoding="utf-8")

    def encode(self: object, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        with lock:
            for text in texts:
                key = text_key(text)
                row = db.execute("SELECT v FROM vectors WHERE k = ?", (key,)).fetchone()
                if row is not None:
                    vectors.append(unpack(row[0]))
                    continue
                missed[0] += 1
                vectors.append(placeholder)
                if key not in listed:
                    listed.add(key)
                    out.write(json.dumps({"k": key, "text": text}) + "\n")
            out.flush()
        return vectors

    onnx_module.OnnxEmbedding._encode = encode
    return missed


# ----------------------------------------------------------------------------- the worker

def _vector_dimension(db: Path) -> int:
    with closing(sqlite3.connect(str(db))) as conn:  # sqlite3's own "with" doesn't close
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'chunks_vec'").fetchone()
    found = re.search(r"float\[(\d+)\]", row[0] if row and row[0] else "")
    return int(found.group(1)) if found else DEFAULT_DIMENSION


def _vector_rows(conn: Any) -> int:
    try:
        return int(conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0])
    except sqlite3.Error:
        return 0


def worker(spec_path: Path) -> int:
    """Index and search each question of a spec; runs with the checkout's ``smriti``."""
    import logging

    logging.basicConfig(level=logging.ERROR)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    run = Path(spec["run"])
    questions = json.loads(Path(spec["questions"]).read_text(encoding="utf-8"))
    missed = (use_vector_cache(Path(spec["vectors"]), Path(spec["misses"]))
              if spec.get("vectors") else [0])

    from smriti.store.indexer import index_tree
    from smriti.store.schema import ensure_schema
    from smriti.store.search import search

    with open(spec["results"], "a", encoding="utf-8") as out:
        for n, q in enumerate(questions, 1):
            root = run / "roots" / re.sub(r"[^A-Za-z0-9_-]", "_", q["question_id"])
            files = session_files(q)
            for f in files:
                path = root / f.rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f.text, encoding="utf-8", newline="\n")
            os.environ["SMRITI_ROOT"] = os.environ["NARADA_ROOT"] = str(root)
            missed_before = missed[0]
            t0 = time.perf_counter()
            stats = index_tree(root=root)
            index_s = time.perf_counter() - t0
            db_path = root / ".smriti" / "index.db"
            conn = ensure_schema(db_path, _vector_dimension(db_path))
            by_source = {f.rel: f.session_id for f in files}
            evidence_of = {f.session_id: f.evidence_lines for f in files}
            evidence = list(q["answer_session_ids"])
            modes: dict[str, Any] = {}
            for mode, use_vectors in MODES:
                t1 = time.perf_counter()
                agent = search(conn, q["question"], top_k=AGENT_TOP_K,
                               candidate_k=AGENT_CANDIDATE_K, use_reranker=False,
                               use_vectors=use_vectors)
                t2 = time.perf_counter()
                deep = search(conn, q["question"], top_k=DEEP_TOP_K,
                              candidate_k=DEEP_CANDIDATE_K, use_reranker=False,
                              use_vectors=use_vectors)
                t3 = time.perf_counter()
                shown = []
                for r in agent:
                    sid = by_source.get(r.source.replace("\\", "/"), "")
                    shown.append({"session": sid, "evidence_turn": sid in evidence and
                                  shows_evidence(r.content, evidence_of.get(sid, ()))})
                modes[mode] = {"agent": shown,
                               "deep": sessions_in_order((r.source for r in deep), by_source),
                               "agent_ms": round((t2 - t1) * 1000, 1),
                               "deep_ms": round((t3 - t2) * 1000, 1)}
            record = {
                "qid": q["question_id"], "type": q["question_type"],
                "abstention": is_abstention(q), "evidence": evidence,
                "evidence_lines": any(evidence_of.get(s) for s in evidence),
                "sessions": len(files), "index_s": round(index_s, 3),
                "index": {k: int(v) for k, v in stats.items()},
                "vector_rows": _vector_rows(conn), "misses": missed[0] - missed_before,
                "modes": modes,
            }
            conn.close()
            out.write(json.dumps(record) + "\n")
            out.flush()
            if not spec.get("keep"):
                shutil.rmtree(root)
            if n % 25 == 0:
                print(f"  {spec['name']}: {n}/{len(questions)}", file=sys.stderr, flush=True)
    return 0


# ----------------------------------------------------------------------------- run

def _commit(checkout: Path) -> str:
    done = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=False)
    return done.stdout.strip() or "unknown"


def worker_env(checkout: Path, run: Path) -> dict[str, str]:
    """The worker's environment: the checkout's code, no service, a runtime dir of its own."""
    env = {k: v for k, v in os.environ.items() if k not in ("SMRITI_ROOT", "NARADA_ROOT")}
    paths = [str(checkout / "src")]
    if (checkout / "profiles" / "reference" / "src").is_dir():
        paths.append(str(checkout / "profiles" / "reference" / "src"))
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["SMRITI_EMBED_SERVICE"] = "0"
    env["SMRITI_EMBED_PROVIDER"] = "onnx"
    env["SMRITI_RUNTIME_DIR"] = str(run / "runtime")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def cmd_run(args: argparse.Namespace) -> int:
    checkout = Path(args.checkout).resolve()
    if not (checkout / "src" / "smriti").is_dir():
        raise SystemExit(f"{checkout} is not a smriti checkout (no src/smriti)")
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    chosen = select(data, args.questions, args.seed)
    del data
    label = args.label or checkout.name
    run = Path(args.work).resolve() / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    run.mkdir(parents=True)
    (run / "runtime").mkdir()
    vectors = str(Path(args.vectors).resolve()) if args.vectors else None
    procs: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    for j in range(args.jobs):
        part = chosen[j::args.jobs]
        if not part:
            continue
        qfile = run / f"questions-{j}.json"
        qfile.write_text(json.dumps(part), encoding="utf-8")
        spec = {"name": f"{label}[{j}]", "run": str(run), "questions": str(qfile),
                "results": str(run / f"results-{j}.jsonl"), "vectors": vectors,
                "misses": str(run / f"misses-{j}.jsonl"), "keep": args.keep}
        spec_path = run / f"spec-{j}.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        log = (run / f"worker-{j}.log").open("w", encoding="utf-8")  # its errors stay here
        logs.append(log)
        procs.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                       "_worker", str(spec_path)],
                                      env=worker_env(checkout, run), stderr=log))
    codes = [p.wait() for p in procs]
    for log in logs:
        log.close()
    for j, code in enumerate(codes):
        if code:
            tail = (run / f"worker-{j}.log").read_text(encoding="utf-8").strip()[-1500:]
            print(f"worker {j} exited {code}:\n{tail}", file=sys.stderr)
    records = load_records(run)
    misses = sum(r["misses"] for r in records)
    meta = {"label": label, "checkout": str(checkout), "commit": _commit(checkout),
            "data": str(Path(args.data).resolve()), "questions": len(chosen),
            "seed": args.seed, "recorded": len(records), "vectors": vectors,
            "misses": misses, "worker_exit": codes,
            "complete": all(c == 0 for c in codes) and len(records) == len(chosen)
            and misses == 0}
    (run / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"{run}: {len(records)}/{len(chosen)} questions, {misses} texts not in the cache, "
          f"{'complete' if meta['complete'] else 'INCOMPLETE'}")
    if misses:
        print(f"  fill the cache: python {Path(__file__).name} embed --vectors {vectors} "
              f"--misses {run}/misses-*.jsonl, then run again")
    return 0 if meta["complete"] else 1


def load_records(run: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(run.glob("results-*.jsonl")):
        with path.open(encoding="utf-8") as f:
            records += [json.loads(line) for line in f if line.strip()]
    return records


# ----------------------------------------------------------------------------- embed

class OnnxEncoder:
    """An ONNX export of the model, encoded as the vendored onnx provider encodes.

    The same tokenizer settings (padding, truncation at 8,192 tokens), and
    the model's pooled output when it has one (``dense_vecs``,
    ``sentence_embedding``), else the CLS token of its last hidden state,
    normalised. ``model_dir`` holds ``model.onnx``
    (with any external data beside it) and ``tokenizer.json``.
    """

    def __init__(self, model_dir: Path, device: str) -> None:
        if device.startswith("cuda"):
            try:  # torch's CUDA and cuDNN libraries, which onnxruntime-gpu can use
                import torch  # noqa: F401
            except ImportError:
                pass
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self.tokenizer.enable_truncation(max_length=8192)
        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                     if device.startswith("cuda") else ["CPUExecutionProvider"])
        self.session = ort.InferenceSession(str(model_dir / "model.onnx"), providers=providers)
        on_gpu = "CUDAExecutionProvider" in self.session.get_providers()
        if device.startswith("cuda") and not on_gpu:
            raise SystemExit("onnxruntime has no CUDA here: install onnxruntime-gpu")
        self.outputs = [o.name for o in self.session.get_outputs()]
        self.inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: list[str]) -> Any:
        import numpy as np

        enc = self.tokenizer.encode_batch(texts)
        feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
        feed = {k: v for k, v in feed.items() if k in self.inputs}
        out = self.session.run(None, feed)
        # The int8 export's pooled output, a sentence-transformers export's,
        # else the CLS token of the last hidden state (as the provider takes it).
        for pooled in ("dense_vecs", "sentence_embedding"):
            if pooled in self.outputs:
                vecs = out[self.outputs.index(pooled)]
                break
        else:
            hidden = next(n for n in ("last_hidden_state", "token_embeddings")
                          if n in self.outputs)
            vecs = out[self.outputs.index(hidden)][:, 0]
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def cmd_embed(args: argparse.Namespace) -> int:
    """Fill the cache with the texts in *misses* lists, with an ONNX export of the model."""
    import numpy as np

    texts: dict[str, str] = {}
    for name in args.misses:
        with open(name, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    texts[rec["k"]] = rec["text"]
    model_dir = Path(args.model)
    encoder = OnnxEncoder(model_dir, args.device)
    dimension = int(encoder.encode(["hello"]).shape[1])
    db = open_cache(Path(args.vectors), dimension, args.model_name or model_dir.name)
    have = {k for (k,) in db.execute("SELECT k FROM vectors")}
    # Shortest first, so each batch pads little.
    todo = sorted(((k, t) for k, t in texts.items() if k not in have), key=lambda kt: len(kt[1]))
    print(f"{len(texts)} texts listed, {len(todo)} not yet in {args.vectors}", flush=True)
    t0 = time.perf_counter()
    for i in range(0, len(todo), args.batch):
        part = todo[i:i + args.batch]
        vecs = encoder.encode([t for _, t in part])
        db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                       [(k, np.asarray(v, dtype="<f4").tobytes())
                        for (k, _), v in zip(part, vecs, strict=True)])
        done = i + len(part)
        if done % (args.batch * 64) < args.batch or done == len(todo):
            db.commit()
            rate = done / (time.perf_counter() - t0)
            print(f"  {done}/{len(todo)} ({rate:.0f} texts/s)", flush=True)
    db.commit()
    db.close()
    return 0


# ----------------------------------------------------------------------------- report

def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)


def cmd_report(args: argparse.Namespace) -> int:
    runs = []
    for name in args.runs:
        run = Path(name)
        meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
        if not meta.get("complete"):
            raise SystemExit(f"{run} is incomplete ({meta.get('misses')} texts not in the "
                             f"cache, {meta.get('recorded')}/{meta.get('questions')} recorded)")
        runs.append((meta, load_records(run)))
    out: dict[str, Any] = {}
    print("| run | mode | n | " + " | ".join(MEASURES) + " |")
    print("|---|---|---|" + "---|" * len(MEASURES))
    for meta, records in runs:
        for mode, _ in MODES:
            s = summarise(records, mode)
            out.setdefault(meta["label"], {})[mode] = s
            row = s.get("all", {"n": 0})
            print(f"| {meta['label']} | {mode} | {row['n']} | "
                  + " | ".join(_fmt(row.get(m)) for m in MEASURES) + " |")
    print()
    for meta, records in runs:
        s = out[meta["label"]]["hybrid"]
        print(f"By question type, {meta['label']}, hybrid:")
        print("| type | n | recall_any@5 | recall_all@10 | ndcg@10 | agent_turn_hit |")
        print("|---|---|---|---|---|---|")
        for group in sorted(g for g in s if g != "all"):
            row = s[group]
            print(f"| {group} | {row['n']} | {_fmt(row['recall_any@5'])} | "
                  f"{_fmt(row['recall_all@10'])} | {_fmt(row['ndcg@10'])} | "
                  f"{_fmt(row['agent_turn_hit'])} |")
        times = [r["modes"]["hybrid"]["agent_ms"] for r in records]
        print(f"\n{meta['label']}: commit {meta['commit'][:12]}, "
              f"vectors {'from ' + Path(meta['vectors']).name if meta['vectors'] else 'computed'}, "
              f"search median {sorted(times)[len(times) // 2]:.0f} ms\n")
    if len(runs) == 2:
        for mode, _ in MODES:
            agree = agreement(runs[0][1], runs[1][1], mode)
            out.setdefault("agreement", {})[mode] = agree
            print(f"Agreement, {runs[0][0]['label']} vs {runs[1][0]['label']}, {mode}: {agree}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


# ----------------------------------------------------------------------------- main

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="index and search each question with one checkout")
    p.add_argument("checkout")
    p.add_argument("--data", required=True, help="longmemeval_s_cleaned.json")
    p.add_argument("--label")
    p.add_argument("--work", default="longmemeval-runs")
    p.add_argument("--questions", type=int, help="a stratified sample of this many")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--vectors", help="a SQLite vector cache (see the module docstring)")
    p.add_argument("--keep", action="store_true", help="keep each question's root")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("embed", help="fill a vector cache from runs' misses")
    p.add_argument("--vectors", required=True)
    p.add_argument("--misses", nargs="+", required=True)
    p.add_argument("--model", required=True,
                   help="a directory with model.onnx and tokenizer.json")
    p.add_argument("--model-name", help="the cache's model name (default: the directory's)")
    p.add_argument("--device", default="cuda")
    p.add_argument("--batch", type=int, default=32)
    p.set_defaults(func=cmd_embed)
    p = sub.add_parser("report", help="the measures of complete runs")
    p.add_argument("runs", nargs="+")
    p.add_argument("--json")
    p.set_defaults(func=cmd_report)
    p = sub.add_parser("_worker")
    p.add_argument("spec")
    p.set_defaults(func=lambda a: worker(Path(a.spec)))
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
