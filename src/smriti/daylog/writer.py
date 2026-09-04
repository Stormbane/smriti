"""Append turns to day-files with dedup, under the writer lock.

Crash safety comes from dedup, not transactions: the capture state may
lag the log, and re-read ranges re-derive identical turn ids that the
writer drops against the day-file's existing ids. Writers never rewrite
a day-file — ordering is the reader's job (render sorts by timestamp).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from smriti.daylog.config import DaylogConfig
from smriti.daylog.lock import writer_lock
from smriti.daylog.model import Turn, day_key
from smriti.daylog.scrub import scrub


def load_day_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                    ids.add(obj["id"])
    except OSError:
        pass
    return ids


def read_day_turns(path: Path) -> list[Turn]:
    """Read a day-file's turns, deduped by id, sorted by event time."""
    turns: dict[str, Turn] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                turn = Turn.from_json(obj)
                if turn is not None and turn.id not in turns:
                    turns[turn.id] = turn
    except OSError:
        pass
    return sorted(turns.values(), key=lambda t: (t.ts, t.id))


def append_turns(cfg: DaylogConfig, turns: list[Turn]) -> tuple[int, set[date]]:
    """Append *turns* to their day-files (event-time placement).

    Takes the writer lock for the duration of the burst. Returns
    ``(appended_count, changed_days)`` — changed days drive the nightly
    repair pass regardless of age (spec: review round 2, finding 2).
    """
    if not turns:
        return 0, set()
    by_day: dict[date, list[Turn]] = {}
    for turn in turns:
        by_day.setdefault(turn.day(), []).append(turn)

    appended = 0
    changed: set[date] = set()
    with writer_lock(cfg.lock_path):
        for day, day_turns in sorted(by_day.items()):
            path = cfg.day_jsonl(day_key(day))
            existing = load_day_ids(path)
            fresh = [t for t in day_turns if t.id not in existing]
            if not fresh:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as f:
                for turn in fresh:
                    record = turn.to_json()
                    record["text"] = scrub(record["text"])
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            appended += len(fresh)
            changed.add(day)
    return appended, changed
