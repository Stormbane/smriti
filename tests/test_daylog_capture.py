"""Capture-path tests for the day-log: adapters, writer, lock, collect."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from smriti.daylog.adapters.claude_jsonl import ClaudeJsonlAdapter
from smriti.daylog.adapters.codex_rollout import CodexRolloutAdapter
from smriti.daylog.adapters.voice_md import VoiceMdAdapter
from smriti.daylog.collect import collect_once
from smriti.daylog.config import DaylogConfig, SourceSpec
from smriti.daylog.lock import LockTimeout, writer_lock
from smriti.daylog.model import Turn, local_day, parse_ts
from smriti.daylog.scrub import scrub
from smriti.daylog.state import DaylogState
from smriti.daylog.writer import append_turns, load_day_ids, read_day_turns


def _cfg(tmp_path: Path, sources: list[SourceSpec] | None = None) -> DaylogConfig:
    return DaylogConfig(root=tmp_path, sources=sources or [])


def _turn(ts: str, text: str = "hello", who: str = "suti", channel: str = "voice") -> Turn:
    parsed = parse_ts(ts)
    assert parsed is not None
    return Turn(ts=parsed, channel=channel, who=who, text=text, session="s1")


# ---------------------------------------------------------------- model


def test_day_placement_brisbane_boundary() -> None:
    # 13:59 UTC = 23:59 Brisbane (same day); 14:01 UTC = 00:01 next day.
    before = _turn("2026-09-04T13:59:00Z")
    after = _turn("2026-09-04T14:01:00Z")
    assert before.day().isoformat() == "2026-09-04"
    assert after.day().isoformat() == "2026-09-05"


def test_turn_id_stable_across_rederivation() -> None:
    a = _turn("2026-09-04T01:00:00Z", "same text")
    b = _turn("2026-09-04T01:00:00Z", "same text")
    assert a.id == b.id
    assert a.id != _turn("2026-09-04T01:00:00Z", "different").id


def test_local_day_handles_naive_timestamps() -> None:
    naive = datetime(2026, 9, 4, 20, 0, 0)  # treated as UTC
    assert local_day(naive).isoformat() == "2026-09-05"


# ---------------------------------------------------------------- scrub


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnop1234",
        "ghp_ABCDEFGHIJKLMNOPQRST1234",
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "api_key = supersecret42",
        "password: hunter2hunter2",
    ],
)
def test_scrub_masks_credentials(secret: str) -> None:
    scrubbed = scrub(f"context {secret} more")
    assert "[redacted]" in scrubbed
    # The secret literal (sans any kept key-name prefix) must be gone.
    tail = secret.split("=")[-1].split(":")[-1].strip()
    assert tail not in scrubbed


def test_scrub_passes_ordinary_text() -> None:
    text = "Is the voice still working? The token endpoint is /talk."
    assert scrub(text) == text


# ------------------------------------------------------- claude adapter


def _claude_line(**over: object) -> bytes:
    rec: dict[str, object] = {
        "type": "user",
        "timestamp": "2026-09-04T11:16:22.859Z",
        "sessionId": "e0903d7c",
        "cwd": "C:\\Users\\admin\\.narada\\chat-sessions\\7487927077",
        "message": {"content": "Is the voice still working?"},
    }
    rec.update(over)
    return json.dumps(rec).encode() + b"\n"


def test_claude_adapter_extracts_and_maps_telegram(tmp_path: Path) -> None:
    path = tmp_path / "e0903d7c.jsonl"
    blob = _claude_line() + _claude_line(
        type="assistant",
        message={"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Let me check the speech system."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        ]},
    )
    result = ClaudeJsonlAdapter().extract(path, blob)
    assert [t.who for t in result.turns] == ["suti", "narada"]
    assert all(t.channel == "telegram" for t in result.turns)
    assert result.turns[1].text == "Let me check the speech system."
    assert result.errors == 0


def test_claude_adapter_channel_from_cwd(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    result = ClaudeJsonlAdapter().extract(path, _claude_line(cwd="C:\\Projects\\smriti"))
    assert result.turns[0].channel == "claude:smriti"


def test_claude_adapter_skips_meta_sidechain_wrappers_and_tools(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    blob = (
        _claude_line(isMeta=True)
        + _claude_line(isSidechain=True)
        + _claude_line(message={"content": "<command-name>/clear</command-name>"})
        + _claude_line(message={"content": [{"type": "tool_result", "content": "out"}]})
        + _claude_line(message={"content": "<system-reminder>injected</system-reminder>"})
        + _claude_line(message={"content": "[Request interrupted by user]"})
        + json.dumps({"type": "progress"}).encode() + b"\n"
        + b"not json at all\n"
    )
    result = ClaudeJsonlAdapter().extract(path, blob)
    assert result.turns == []
    assert result.errors == 1  # only the malformed line counts


def test_claude_adapter_leaves_partial_tail(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    full = _claude_line()
    blob = full + b'{"type":"user","time'
    result = ClaudeJsonlAdapter().extract(path, blob)
    assert result.consumed == len(full)
    assert len(result.turns) == 1


# -------------------------------------------------------- codex adapter


def _codex_line(ptype: str, message: str, phase: str | None = None) -> bytes:
    payload: dict[str, object] = {"type": ptype, "message": message}
    if phase is not None:
        payload["phase"] = phase
    return json.dumps(
        {"timestamp": "2026-07-15T13:10:02.435Z", "type": "event_msg", "payload": payload}
    ).encode() + b"\n"


def test_codex_adapter_keeps_conversation_drops_commentary(tmp_path: Path) -> None:
    path = tmp_path / "rollout-2026-07-15T23-06-13-019f65e2-8c69-7ec1-9efd-8fe3de509306.jsonl"
    meta = json.dumps(
        {"type": "session_meta", "timestamp": "2026-07-15T13:10:02.130Z",
         "payload": {"cwd": "C:\\Projects\\beautiful-tree"}}
    ).encode() + b"\n"
    path.write_bytes(meta)
    blob = (
        meta
        + _codex_line("user_message", "hi")
        + _codex_line("agent_message", "getting oriented", phase="commentary")
        + _codex_line("agent_message", "Hi Suti — what shall we work on?", phase="final_answer")
        + json.dumps({"type": "response_item", "payload": {}}).encode() + b"\n"
    )
    result = CodexRolloutAdapter().extract(path, blob)
    assert [(t.who, t.text) for t in result.turns] == [
        ("suti", "hi"),
        ("narada", "Hi Suti — what shall we work on?"),
    ]
    assert all(t.channel == "codex:beautiful-tree" for t in result.turns)
    assert result.turns[0].session == "019f65e2"


# -------------------------------------------------------- voice adapter


def test_voice_adapter_parses_turns_and_midnight_rollover(tmp_path: Path) -> None:
    path = tmp_path / "20260903-235950-narada-body-sim.md"
    blob = (
        "# Voice transcript — narada-body-sim\n"
        "started: 2026-09-03T23:59:50.000000+00:00\n"
        "> RECORDING ACTIVE — this conversation is being logged.\n"
        "- `23:59:55` **user:** hello there\n"
        "- `00:00:10` **assistant:** Hey Suti.\n"
    ).encode()
    result = VoiceMdAdapter().extract(path, blob)
    assert len(result.turns) == 2
    assert result.turns[0].ts.day == 3
    assert result.turns[1].ts.day == 4  # crossed midnight UTC
    assert result.turns[0].who == "suti"
    assert result.turns[1].channel == "voice"
    assert result.errors == 0


# --------------------------------------------------------------- writer


def test_append_dedups_and_reports_changed_days(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    turns = [_turn("2026-09-04T01:00:00Z", "a"), _turn("2026-09-04T14:01:00Z", "b")]
    appended, changed = append_turns(cfg, turns)
    assert appended == 2
    assert {d.isoformat() for d in changed} == {"2026-09-04", "2026-09-05"}
    # Re-appending the same turns (crash re-read) is a no-op.
    appended2, changed2 = append_turns(cfg, turns)
    assert appended2 == 0 and changed2 == set()
    assert len(load_day_ids(cfg.day_jsonl("2026-09-04"))) == 1


def test_read_day_turns_sorts_by_event_time(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    late = _turn("2026-09-04T05:00:00Z", "later")
    early = _turn("2026-09-04T01:00:00Z", "earlier")
    append_turns(cfg, [late])
    append_turns(cfg, [early])
    texts = [t.text for t in read_day_turns(cfg.day_jsonl("2026-09-04"))]
    assert texts == ["earlier", "later"]


def test_writer_scrubs_before_persistence(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    append_turns(cfg, [_turn("2026-09-04T01:00:00Z", "key sk-ant-api03-abcdefghijklmnop1234")])
    raw = cfg.day_jsonl("2026-09-04").read_text(encoding="utf-8")
    assert "sk-ant" not in raw and "[redacted]" in raw


# ----------------------------------------------------------------- lock


def test_writer_lock_excludes_and_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "w.lock"
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with writer_lock(lock_path):
            entered.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    assert entered.wait(timeout=5)
    with pytest.raises(LockTimeout):
        with writer_lock(lock_path, timeout_s=0.3):
            pass
    release.set()
    t.join(timeout=5)
    with writer_lock(lock_path, timeout_s=1.0):
        pass  # released cleanly


def test_writer_lock_breaks_stale_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "w.lock"
    lock_path.write_text("999999999", encoding="ascii")  # dead pid
    with writer_lock(lock_path, timeout_s=2.0):
        assert lock_path.read_text(encoding="ascii") == str(os.getpid())


# -------------------------------------------------------------- collect


def _voice_source(tmp_path: Path) -> tuple[DaylogConfig, Path]:
    src_dir = tmp_path / "vt" / "2026_09"
    src_dir.mkdir(parents=True)
    spec = SourceSpec(name="voice", kind="voice_md", glob=str(tmp_path / "vt" / "*" / "*.md"))
    return _cfg(tmp_path, [spec]), src_dir


def test_collect_tails_incrementally_without_duplicates(tmp_path: Path) -> None:
    cfg, src_dir = _voice_source(tmp_path)
    f = src_dir / "20260904-100000-box.md"
    f.write_text("- `10:00:05` **user:** first\n", encoding="utf-8")
    state = DaylogState.load(cfg.state_path)
    r1 = collect_once(cfg, state)
    assert r1.appended == 1
    with f.open("a", encoding="utf-8") as fh:
        fh.write("- `10:00:09` **assistant:** second\n")
    r2 = collect_once(cfg, state)
    assert r2.appended == 1
    turns = read_day_turns(cfg.day_jsonl("2026-09-04"))
    assert [t.text for t in turns] == ["first", "second"]


def test_collect_crash_between_append_and_state_save(tmp_path: Path) -> None:
    """Marks lagging the log (crash) re-reads a range; dedup absorbs it."""
    cfg, src_dir = _voice_source(tmp_path)
    f = src_dir / "20260904-100000-box.md"
    f.write_text("- `10:00:05` **user:** once only\n", encoding="utf-8")
    stale = DaylogState.load(cfg.state_path)  # loaded before the first pass
    live = DaylogState.load(cfg.state_path)
    assert collect_once(cfg, live).appended == 1
    # "Crash": rerun the pass with the stale (pre-append) state.
    assert collect_once(cfg, stale).appended == 0
    assert len(read_day_turns(cfg.day_jsonl("2026-09-04"))) == 1


def test_collect_reread_on_truncation(tmp_path: Path) -> None:
    cfg, src_dir = _voice_source(tmp_path)
    f = src_dir / "20260904-100000-box.md"
    f.write_text("- `10:00:05` **user:** original\n", encoding="utf-8")
    state = DaylogState.load(cfg.state_path)
    collect_once(cfg, state)
    # Recreate the file with different content (identity change).
    f.write_text("- `10:00:07` **user:** rewritten\n", encoding="utf-8")
    collect_once(cfg, state)
    texts = {t.text for t in read_day_turns(cfg.day_jsonl("2026-09-04"))}
    assert texts == {"original", "rewritten"}


def test_collect_health_tracks_scan_recency(tmp_path: Path) -> None:
    cfg, _src_dir = _voice_source(tmp_path)
    state = DaylogState.load(cfg.state_path)
    collect_once(cfg, state)
    health = state.health()
    assert health["voice"]["scan_age_s"] >= 0  # scanned, even with no turns
    assert health["voice"]["turn_age_s"] == -1  # quiet source, distinguishable
    time.sleep(0.01)
    assert DaylogState.load(cfg.state_path).sources["voice"].last_scan > 0
