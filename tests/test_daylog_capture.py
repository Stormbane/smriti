"""Capture-path tests for the day-log: adapters, writer, lock, collect."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smriti.daylog.adapters.claude_jsonl import ClaudeJsonlAdapter
from smriti.daylog.adapters.codex_rollout import CodexRolloutAdapter
from smriti.daylog.adapters.voice_md import VoiceMdAdapter
from smriti.daylog.collect import collect_once
from smriti.daylog.config import DaylogConfig, SourceSpec
from smriti.daylog.lock import LockTimeout, writer_lock
from smriti.daylog.model import AGENT, USER, Turn, local_day, make_turn_id, parse_ts
from smriti.daylog.scrub import scrub
from smriti.daylog.state import DaylogState
from smriti.daylog.writer import append_turns, load_day_ids, read_day_turns


def _cfg(tmp_path: Path, sources: list[SourceSpec] | None = None) -> DaylogConfig:
    return DaylogConfig(root=tmp_path, sources=sources or [])


def _turn(ts: str, text: str = "hello", who: str = USER, channel: str = "voice") -> Turn:
    parsed = parse_ts(ts)
    assert parsed is not None
    return Turn(ts=parsed, channel=channel, who=who, text=text, session="s1")


# ---------------------------------------------------------------- model


def test_day_placement_utc_plus_10_boundary() -> None:
    # 13:59 UTC = 23:59 UTC+10 (same day); 14:01 UTC = 00:01 next day.
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


# Ids the code minted before the speakers became roles, recorded from a run of
# that code: make_turn_id("claude:proj", "abcdef1234", _ID_TS, "suti"|"narada", ...).
_ID_TS = datetime(2026, 11, 3, 9, 15, 30, tzinfo=timezone.utc)
_LEGACY_IDS = {
    (USER, "Hello there"): "claude:proj:abcdef12:2245e5b2b10b",
    (AGENT, "Hi!"): "claude:proj:abcdef12:3a1498b2078d",
}


def _legacy_turn_id(channel: str, session: str, ts: datetime, who: str, text: str) -> str:
    """The id formula before roles: the speaker's name hashed as stored."""
    stamp = ts.astimezone(timezone.utc).isoformat()
    digest = hashlib.sha1(f"{stamp}|{who}|{text}".encode("utf-8")).hexdigest()[:12]
    return f"{channel}:{(session or '-')[:8]}:{digest}"


@pytest.mark.parametrize("role,legacy", [(USER, "suti"), (AGENT, "narada")])
def test_a_role_turn_keeps_the_id_its_legacy_name_had(role: str, legacy: str) -> None:
    text = "Hello there" if role == USER else "Hi!"
    expected = _LEGACY_IDS[(role, text)]
    assert _legacy_turn_id("claude:proj", "abcdef1234", _ID_TS, legacy, text) == expected
    assert make_turn_id("claude:proj", "abcdef1234", _ID_TS, role, text) == expected
    assert make_turn_id("claude:proj", "abcdef1234", _ID_TS, legacy, text) == expected
    turn = Turn(ts=_ID_TS, channel="claude:proj", who=role, text=text, session="abcdef1234")
    assert turn.id == expected


def test_legacy_who_is_read_as_its_role() -> None:
    stored = {"id": "voice:s1:0123456789ab", "ts": "2026-09-04T01:00:00Z", "channel": "voice",
              "who": "suti", "text": "hi", "session": "s1"}
    turn = Turn.from_json(stored)
    assert turn is not None and turn.who == USER and turn.id == stored["id"]
    reply = Turn.from_json({**stored, "who": "narada", "id": ""})
    assert reply is not None and reply.who == AGENT
    assert reply.to_json()["who"] == AGENT  # new records carry the role


def test_recapturing_a_legacy_day_appends_nothing(tmp_path: Path) -> None:
    """A day written with ``who: "suti"`` dedups against the same turn captured as a role."""
    cfg = _cfg(tmp_path)
    ts = parse_ts("2026-09-04T01:00:00Z")
    assert ts is not None
    legacy = {"id": _legacy_turn_id("voice", "s1", ts, "suti", "hello"),
              "ts": "2026-09-04T01:00:00Z", "channel": "voice", "who": "suti",
              "text": "hello", "session": "s1"}
    path = cfg.day_jsonl("2026-09-04")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    appended, _ = append_turns(cfg, [_turn("2026-09-04T01:00:00Z", "hello", who=USER)])
    assert appended == 0
    assert [t.who for t in read_day_turns(path)] == [USER]


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
        "github_pat_" + "11ABCDEFG0123456789_abcdefghij",
        "sk_" + "live_" + "51HxABCDEFGHIJKLMNOPQRSTUV",
        "rk_" + "test_" + "51HxABCDEFGHIJKLMNOPQRSTUV",
        "AIza" + "SyA1234567890abcdefghijklmnopqrstuv",
        "npm_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "123456789:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawB",
        "https://hooks.slack.com/" + "services/T000/B000/XXXXXXXXXXXXXXXX",
        "eyJhbGciOiJIUzI1NiJ9."
        + "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_scrub_masks_credentials(secret: str) -> None:
    scrubbed = scrub(f"context {secret} more")
    assert "[redacted]" in scrubbed
    # The secret literal (sans any kept key-name prefix) must be gone.
    tail = secret.split("=")[-1].split(":")[-1].strip()
    assert tail not in scrubbed


def test_scrub_masks_only_the_password_in_a_url() -> None:
    scrubbed = scrub("connect to postgres://app:s3cr3tpass@db.example.com:5432/main now")
    assert scrubbed == "connect to postgres://app:[redacted]@db.example.com:5432/main now"


def test_scrub_passes_ordinary_text() -> None:
    text = "Is the voice still working? The token endpoint is /talk."
    assert scrub(text) == text


# ------------------------------------------------------- claude adapter


def _claude_line(**over: object) -> bytes:
    rec: dict[str, object] = {
        "type": "user",
        "timestamp": "2026-09-04T11:00:00.000Z",
        "sessionId": "a1b2c3d4",
        "cwd": "C:\\Users\\tester\\.narada\\chat-sessions\\1000000001",
        "message": {"content": "Is the voice still working?"},
    }
    rec.update(over)
    return json.dumps(rec).encode() + b"\n"


def test_claude_adapter_extracts_and_maps_telegram(tmp_path: Path) -> None:
    path = tmp_path / "a1b2c3d4.jsonl"
    blob = _claude_line() + _claude_line(
        type="assistant",
        message={"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Let me check the speech system."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        ]},
    )
    result = ClaudeJsonlAdapter().extract(path, blob)
    assert [t.who for t in result.turns] == [USER, AGENT]
    assert all(t.channel == "telegram" for t in result.turns)
    assert result.turns[1].text == "Let me check the speech system."
    assert result.errors == 0


@pytest.mark.parametrize("cwd,project", [
    ("C:\\Projects\\smriti", "smriti"), ("C:\\Projects\\smriti\\", "smriti"),
    ("/home/ari/code/smriti", "smriti"), ("/home/ari/code/smriti/", "smriti"),
])
def test_project_of_any_platforms_cwd(cwd: str, project: str) -> None:
    from smriti.daylog.adapters import project_of

    assert project_of(cwd) == project


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
    path = tmp_path / "rollout-2026-07-15T23-06-13-01900000-0000-7000-8000-000000000001.jsonl"
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
        (USER, "hi"),
        (AGENT, "Hi Suti — what shall we work on?"),
    ]
    assert all(t.channel == "codex:beautiful-tree" for t in result.turns)
    assert result.turns[0].session == "01900000"


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
    assert result.turns[0].who == USER
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


def test_writer_lock_survives_a_crashed_holder(tmp_path: Path) -> None:
    """The OS releases the lock when its holder dies, however it dies."""
    import subprocess
    import sys

    lock_path = tmp_path / "w.lock"
    code = "\n".join([
        "import os, sys",
        "from pathlib import Path",
        "from smriti.daylog.lock import writer_lock",
        "with writer_lock(Path(sys.argv[1])):",
        "    os._exit(0)  # die holding it, no cleanup",
    ])
    subprocess.run([sys.executable, "-c", code, str(lock_path)], check=True, timeout=60)
    with writer_lock(lock_path, timeout_s=2.0):
        pass


def test_writer_lock_is_never_broken_by_age(tmp_path: Path) -> None:
    """A long holder keeps the lock, however old its lock file looks."""
    from smriti.core.oslock import lock_path as os_lock_path

    lock_path = tmp_path / "w.lock"
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        with writer_lock(lock_path):
            entered.set()
            release.wait(timeout=10)

    t = threading.Thread(target=hold)
    t.start()
    assert entered.wait(timeout=5)
    key = os_lock_path("daylog-writer", str(lock_path.resolve()))
    old = time.time() - 3600
    os.utime(key, (old, old))  # the old implementation broke anything over 300 s
    with pytest.raises(LockTimeout):
        with writer_lock(lock_path, timeout_s=0.3):
            pass
    release.set()
    t.join(timeout=5)
    assert not lock_path.exists(), "no lock file inside the tree"


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
    r1 = collect_once(cfg)
    assert r1.appended == 1
    with f.open("a", encoding="utf-8") as fh:
        fh.write("- `10:00:09` **assistant:** second\n")
    r2 = collect_once(cfg)
    assert r2.appended == 1
    turns = read_day_turns(cfg.day_jsonl("2026-09-04"))
    assert [t.text for t in turns] == ["first", "second"]


def test_collect_crash_between_append_and_state_save(tmp_path: Path) -> None:
    """Marks lagging the log (crash) re-reads a range; dedup absorbs it."""
    cfg, src_dir = _voice_source(tmp_path)
    f = src_dir / "20260904-100000-box.md"
    f.write_text("- `10:00:05` **user:** once only\n", encoding="utf-8")
    assert collect_once(cfg).appended == 1
    # "Crash": the state save never landed — marks regress to zero.
    cfg.state_path.unlink()
    assert collect_once(cfg).appended == 0
    assert len(read_day_turns(cfg.day_jsonl("2026-09-04"))) == 1


def test_collect_reread_on_truncation(tmp_path: Path) -> None:
    cfg, src_dir = _voice_source(tmp_path)
    f = src_dir / "20260904-100000-box.md"
    f.write_text("- `10:00:05` **user:** original\n", encoding="utf-8")
    collect_once(cfg)
    # Recreate the file with different content (identity change).
    f.write_text("- `10:00:07` **user:** rewritten\n", encoding="utf-8")
    collect_once(cfg)
    texts = {t.text for t in read_day_turns(cfg.day_jsonl("2026-09-04"))}
    assert texts == {"original", "rewritten"}


def test_collect_health_tracks_scan_recency(tmp_path: Path) -> None:
    cfg, _src_dir = _voice_source(tmp_path)
    collect_once(cfg)
    health = DaylogState.load(cfg.state_path).health()
    assert health["voice"]["scan_age_s"] >= 0  # scanned, even with no turns
    assert health["voice"]["turn_age_s"] == -1  # quiet source, distinguishable
    time.sleep(0.01)
    assert DaylogState.load(cfg.state_path).sources["voice"].last_scan > 0


def test_collect_marks_dirty_days_until_cleared(tmp_path: Path) -> None:
    cfg, src_dir = _voice_source(tmp_path)
    f = src_dir / "20260901-100000-box.md"
    f.write_text("- `10:00:05` **user:** old day turn\n", encoding="utf-8")
    collect_once(cfg)
    assert DaylogState.load(cfg.state_path).dirty_days == {"2026-09-01"}


def test_collect_excludes_llm_workdir_transcripts(tmp_path: Path) -> None:
    src_dir = tmp_path / "projects" / "C--Users-admin--narada--smriti-llm-workdir"
    src_dir.mkdir(parents=True)
    spec = SourceSpec(name="claude", kind="claude_jsonl",
                      glob=str(tmp_path / "projects" / "*" / "*.jsonl"))
    cfg = _cfg(tmp_path, [spec])
    (src_dir / "s.jsonl").write_bytes(_claude_line(cwd=""))
    assert collect_once(cfg).appended == 0


def test_collect_runs_while_lock_held_only_after_release(tmp_path: Path) -> None:
    """A concurrent writer-lock holder delays but does not corrupt a pass."""
    cfg, src_dir = _voice_source(tmp_path)
    (src_dir / "20260904-100000-box.md").write_text(
        "- `10:00:05` **user:** locked run\n", encoding="utf-8"
    )
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with writer_lock(cfg.lock_path):
            entered.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    assert entered.wait(timeout=5)
    with pytest.raises(LockTimeout):
        collect_once(cfg, lock_timeout_s=0.3)
    release.set()
    t.join(timeout=5)
    assert collect_once(cfg).appended == 1


def test_config_explicit_empty_sources_disable_defaults(tmp_path: Path) -> None:
    from smriti.daylog.config import load_config

    smriti_dir = tmp_path / ".smriti"
    smriti_dir.mkdir()
    (smriti_dir / "daylog.json").write_text('{"sources": []}', encoding="utf-8")
    assert load_config(tmp_path).sources == []


@pytest.mark.parametrize("without_psutil", [False, True])
def test_lock_pid_alive_probe_is_side_effect_free(monkeypatch: pytest.MonkeyPatch,
                                                  without_psutil: bool) -> None:
    import subprocess
    import sys

    from smriti.daylog import lock

    if without_psutil:  # the fallback path that fresh Windows installs take
        monkeypatch.setitem(sys.modules, "psutil", None)
    assert lock._pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not lock._pid_alive(proc.pid)
    assert not lock._pid_alive(0)
