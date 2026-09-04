"""Adapter for prana voice transcripts.

Source: ``<tree>/heartbeat/voice-transcripts/YYYY_MM/<stamp>-<room>.md``
— markdown written live by the voice worker (redaction already applied
upstream at write time). Turn lines look like:

    - `16:23:20` **assistant:** Hey Suti, ...
    - `16:23:31` **user:** ...

Line times are UTC clock times; the date comes from the filename stamp
(``YYYYMMDD-HHMMSS-...``). A line whose clock time is earlier than the
session start is assumed to have crossed midnight UTC.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from smriti.daylog.adapters import ExtractResult, complete_lines
from smriti.daylog.model import Turn

_NAME_RE = re.compile(r"^(\d{8})-(\d{6})-(.+)\.md$")
# Real lines put the colon inside the bold ("**assistant:**"); accept it
# on either side of the closing marker.
_TURN_RE = re.compile(r"^-\s+`(\d{2}):(\d{2}):(\d{2})`\s+\*\*(user|assistant):?\*\*:?\s*(.+)$")


def _session_start(path: Path) -> tuple[datetime, str] | None:
    m = _NAME_RE.match(path.name)
    if not m:
        return None
    stamp, clock, room = m.groups()
    try:
        start = datetime.strptime(f"{stamp}{clock}", "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return start, room


class VoiceMdAdapter:
    def extract(self, path: Path, blob: bytes) -> ExtractResult:
        result = ExtractResult()
        lines, result.consumed = complete_lines(blob)
        meta = _session_start(path)
        if meta is None:
            if lines:
                result.errors += 1
            return result
        start, _room = meta
        session = path.stem[:15]
        for raw in lines:
            text_line = raw.decode("utf-8", errors="replace").rstrip("\r")
            m = _TURN_RE.match(text_line)
            if not m:
                continue  # headers, markers, blank lines — not errors
            hh, mm, ss, speaker, text = m.groups()
            try:
                clock = time(int(hh), int(mm), int(ss))
            except ValueError:
                result.errors += 1
                continue
            ts = datetime.combine(start.date(), clock, tzinfo=timezone.utc)
            if ts < start - timedelta(minutes=1):
                ts += timedelta(days=1)  # crossed midnight UTC
            result.turns.append(
                Turn(
                    ts=ts,
                    channel="voice",
                    who="suti" if speaker == "user" else "narada",
                    text=text.strip(),
                    session=session,
                )
            )
        return result
