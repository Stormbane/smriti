"""Per-source capture state, persisted crash-safe.

For every source file we track identity (first-line fingerprint + last
size), the consumed byte offset, and health counters. Marks may lag the
log (crash between append and state save): re-reads then re-emit turns
whose ids the writer already holds, and dedup absorbs them — that is
the at-least-once/exactly-once contract.

Health is defined by *scan* recency, not append recency, so a
legitimately quiet channel is distinguishable from a stalled tail
(spec: review round 1, finding 9).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

_FINGERPRINT_BYTES = 256


@dataclass
class FileState:
    offset: int = 0
    size: int = 0
    fingerprint: str = ""
    last_scan: float = 0.0
    last_turn: float = 0.0
    parse_errors: int = 0


@dataclass
class SourceState:
    files: dict[str, FileState] = field(default_factory=dict)
    last_scan: float = 0.0
    last_turn: float = 0.0
    parse_errors: int = 0


def fingerprint_head(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(_FINGERPRINT_BYTES)
    except OSError:
        return ""
    return hashlib.sha1(head).hexdigest()[:16]


class DaylogState:
    """Load/mutate/save the whole capture state atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.sources: dict[str, SourceState] = {}

    @classmethod
    def load(cls, path: Path) -> "DaylogState":
        state = cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return state
        for name, src in raw.get("sources", {}).items():
            if not isinstance(src, dict):
                continue
            source = SourceState(
                last_scan=float(src.get("last_scan", 0.0)),
                last_turn=float(src.get("last_turn", 0.0)),
                parse_errors=int(src.get("parse_errors", 0)),
            )
            for fpath, fstate in (src.get("files") or {}).items():
                if isinstance(fstate, dict):
                    source.files[fpath] = FileState(
                        offset=int(fstate.get("offset", 0)),
                        size=int(fstate.get("size", 0)),
                        fingerprint=str(fstate.get("fingerprint", "")),
                        last_scan=float(fstate.get("last_scan", 0.0)),
                        last_turn=float(fstate.get("last_turn", 0.0)),
                        parse_errors=int(fstate.get("parse_errors", 0)),
                    )
            state.sources[name] = source
        return state

    def source(self, name: str) -> SourceState:
        return self.sources.setdefault(name, SourceState())

    def file(self, source: str, path: Path) -> FileState:
        return self.source(source).files.setdefault(str(path), FileState())

    def note_scan(self, source: str) -> None:
        self.source(source).last_scan = time.time()

    def save(self) -> None:
        payload = {"sources": {n: asdict(s) for n, s in self.sources.items()}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def health(self) -> dict[str, dict[str, float | int]]:
        """Per-source health summary for `smriti status` / wake briefing."""
        now = time.time()
        out: dict[str, dict[str, float | int]] = {}
        for name, src in self.sources.items():
            out[name] = {
                "scan_age_s": round(now - src.last_scan, 1) if src.last_scan else -1,
                "turn_age_s": round(now - src.last_turn, 1) if src.last_turn else -1,
                "parse_errors": src.parse_errors,
                "files": len(src.files),
            }
        return out
