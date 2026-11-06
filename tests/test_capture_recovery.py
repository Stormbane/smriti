"""Capture recovers from bad input and from crashes (H12.6).

A torn last line in a day-file is closed before the next append, so the
records after it stay whole; a capture killed mid-append, or between its
append and its state save, loses and duplicates no turn when the next
pass runs. These run the real code in a child process and kill it with
``os._exit``, as a crash would.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from smriti.daylog.collect import collect_once
from smriti.daylog.config import DaylogConfig, SourceSpec
from smriti.daylog.model import USER, Turn, parse_ts
from smriti.daylog.state import DaylogState
from smriti.daylog.writer import (
    AppendPass,
    append_turns,
    append_turns_unlocked,
    read_day_turns,
    scan_day,
)

DAY = "2026-09-04"


def _turn(minute: int, text: str) -> Turn:
    ts = parse_ts(f"2026-09-04T01:{minute:02d}:00Z")
    assert ts is not None
    return Turn(ts=ts, channel="claude:orchard", who=USER, text=text, session="s1")


def _claude(minute: int, text: str, who: str = "user") -> str:
    message: object = text if who == "user" else [{"type": "text", "text": text}]
    return json.dumps({"type": who, "timestamp": f"2026-09-04T01:{minute:02d}:00.000Z",
                       "sessionId": "5a3e0001", "cwd": "/home/sam/projects/orchard",
                       "message": {"content": message}}) + "\n"


def _source(tmp_path: Path) -> tuple[DaylogConfig, Path]:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    spec = SourceSpec(name="claude", kind="claude_jsonl", glob=str(transcripts / "*.jsonl"))
    root = tmp_path / "root"
    root.mkdir()
    return DaylogConfig(root=root, sources=[spec]), transcripts / "5a3e0001.jsonl"


def _say(path: Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write("".join(lines))


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _records(path: Path) -> list[dict[str, object]]:
    """Every line that is a record; a line holding two glued records isn't one."""
    out = []
    for line in _lines(path):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _run_child(code: str, cfg: DaylogConfig) -> int:
    """Run *code* in a fresh interpreter with ``cfg`` rebuilt as ``cfg``; returns its exit code."""
    spec = cfg.sources[0]
    prelude = "\n".join([
        "import os, sys",
        "from pathlib import Path",
        "from smriti.daylog.collect import collect_once",
        "from smriti.daylog.config import DaylogConfig, SourceSpec",
        "cfg = DaylogConfig(root=Path(sys.argv[1]),",
        "                   sources=[SourceSpec(name='claude', kind='claude_jsonl',",
        "                                       glob=sys.argv[2])])",
    ])
    done = subprocess.run([sys.executable, "-c", prelude + "\n" + code, str(cfg.root), spec.glob],
                          capture_output=True, text=True, timeout=120)
    return done.returncode


# ------------------------------------------------------------ torn last line


def test_a_torn_last_line_is_closed_before_the_next_append(tmp_path: Path) -> None:
    cfg = DaylogConfig(root=tmp_path)
    append_turns(cfg, [_turn(1, "first")])
    path = cfg.day_jsonl(DAY)
    whole = path.read_bytes()
    second = json.dumps(_turn(2, "second, cut short").to_json()).encode()
    path.write_bytes(whole + second[: len(second) // 2])  # an append that didn't finish

    batch = AppendPass()
    from smriti.daylog.lock import writer_lock

    with writer_lock(cfg.lock_path):
        appended, _ = append_turns_unlocked(cfg, [_turn(3, "third")], batch)
        batch.sync()
    assert appended == 1
    lines = _lines(path)
    assert len(lines) == 3 and json.loads(lines[2])["text"] == "third"
    assert [t.text for t in read_day_turns(path)] == ["first", "third"]
    assert batch.repaired == [DAY] and batch.invalid == {DAY: 1} and not batch.unsynced
    scan = scan_day(path)
    assert scan.invalid == 1 and not scan.torn


def test_without_the_repair_the_next_record_was_lost_with_the_fragment() -> None:
    """What the repair prevents: a record glued onto a fragment parses as neither."""
    glued = '{"id": "a", "te' + json.dumps(_turn(3, "third").to_json())
    with pytest.raises(json.JSONDecodeError):
        json.loads(glued)


def test_a_whole_record_missing_only_its_newline_keeps_its_place(tmp_path: Path) -> None:
    cfg = DaylogConfig(root=tmp_path)
    path = cfg.day_jsonl(DAY)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_turn(1, "first").to_json()), encoding="utf-8")  # no newline
    assert append_turns(cfg, [_turn(1, "first"), _turn(2, "second")])[0] == 1
    assert [json.loads(line)["text"] for line in _lines(path)] == ["first", "second"]
    assert scan_day(path).invalid == 0


def test_the_same_turn_twice_in_one_batch_is_written_once(tmp_path: Path) -> None:
    cfg = DaylogConfig(root=tmp_path)
    assert append_turns(cfg, [_turn(1, "once"), _turn(1, "once")])[0] == 1
    assert len(_lines(cfg.day_jsonl(DAY))) == 1


# ------------------------------------------------------------ fault injection


def test_a_capture_killed_mid_append_loses_no_turn(tmp_path: Path) -> None:
    cfg, transcript = _source(tmp_path)
    texts = [f"turn {i} about the orchard ledger" for i in range(6)]
    _say(transcript, *(_claude(i, text) for i, text in enumerate(texts)))
    code = "\n".join([
        "from smriti.daylog import writer",
        "def torn(f, data):",
        "    cut = 0",
        "    for _ in range(3):",
        "        cut = data.index(b'\\n', cut) + 1",
        "    f.write(data[:cut + 10])  # three records and the start of a fourth",
        "    f.flush()",
        "    os._exit(17)",
        "writer._append_bytes = torn",
        "collect_once(cfg)",
    ])
    assert _run_child(code, cfg) == 17
    path = cfg.day_jsonl(DAY)
    assert len(_records(path)) == 3 and not path.read_bytes().endswith(b"\n")
    assert not cfg.state_path.exists()  # it died before saving any offset

    report = collect_once(cfg)  # the restart
    assert report.appended == 3 and report.repaired_days == [DAY]
    assert [t.text for t in read_day_turns(path)] == texts
    assert len(_records(path)) == 6 and len(_lines(path)) == 7  # six records, one fragment
    state = DaylogState.load(cfg.state_path)
    assert state.corrupt_lines == {DAY: 1} and state.torn_repairs == 1

    _say(transcript, _claude(7, "and one more"))
    assert collect_once(cfg).appended == 1
    assert [t.text for t in read_day_turns(path)] == [*texts, "and one more"]


@pytest.mark.parametrize("crash", ["before the state is saved", "while the state is saved"])
def test_a_capture_killed_after_its_append_duplicates_no_turn(tmp_path: Path, crash: str) -> None:
    cfg, transcript = _source(tmp_path)
    _say(transcript, _claude(0, "already captured"), _claude(1, "and this", who="assistant"))
    assert collect_once(cfg).appended == 2
    offset = DaylogState.load(cfg.state_path).sources["claude"].files[str(transcript)].offset
    _say(transcript, *(_claude(i, f"new turn {i}") for i in range(2, 6)))
    if crash == "before the state is saved":
        hook = ("from smriti.daylog.state import DaylogState\n"
                "DaylogState.save = lambda self: os._exit(18)")
    else:  # the new state is written to its temp file, and the process dies before the rename
        hook = "import pathlib\npathlib.Path.replace = lambda self, target: os._exit(18)"
    assert _run_child(hook + "\ncollect_once(cfg)", cfg) == 18
    path = cfg.day_jsonl(DAY)
    assert len(_records(path)) == 6  # the child appended all four
    saved = DaylogState.load(cfg.state_path).sources["claude"].files[str(transcript)].offset
    assert saved == offset  # and never saved the offset past them

    assert collect_once(cfg).appended == 0  # the replay re-derives the same ids
    assert len(_lines(path)) == 6 and len({r["id"] for r in _records(path)}) == 6
    _say(transcript, _claude(7, "after the crash"))
    assert collect_once(cfg).appended == 1
    assert read_day_turns(path)[-1].text == "after the crash"


# ------------------------------------------------------------ state and status


def test_a_state_entry_of_the_wrong_kind_is_read_again_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / ".state.json"
    path.write_text(json.dumps({"sources": {"claude": {"files": {
        "/a.jsonl": {"offset": "not a number"}, "/b.jsonl": {"offset": 7, "size": 9}}}},
        "corrupt_lines": {"2026-09-04": 2, "2026-09-05": "x"}}), encoding="utf-8")
    state = DaylogState.load(path)
    assert list(state.sources["claude"].files) == ["/b.jsonl"]
    assert state.corrupt_lines == {"2026-09-04": 2}
    for junk in ("[]", "null", '{"sources": []}', "\x00\x00"):
        path.write_text(junk, encoding="utf-8")
        assert DaylogState.load(path).sources == {}


def test_a_healthy_state_file_carries_no_day_log_health(tmp_path: Path) -> None:
    cfg, transcript = _source(tmp_path)
    _say(transcript, _claude(0, "hello"))
    collect_once(cfg)
    saved = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert "corrupt_lines" not in saved and "torn_repairs" not in saved


def test_status_says_how_many_corrupt_lines_the_day_log_holds(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from smriti.cli import main
    from smriti.core.tree import tree_root

    root = tree_root()
    (root / ".smriti").mkdir(parents=True, exist_ok=True)
    (root / ".smriti" / "daylog.json").write_text(json.dumps({"sources": [
        {"name": "claude", "kind": "claude_jsonl", "glob": str(tmp_path / "t" / "*.jsonl")}]}),
        encoding="utf-8")
    cfg = DaylogConfig(root=root)
    append_turns(cfg, [_turn(1, "first")])
    path = cfg.day_jsonl(DAY)
    path.write_bytes(path.read_bytes() + b'{"id": "torn')
    collect = DaylogConfig(root=root, sources=[SourceSpec("claude", "claude_jsonl",
                                                          str(tmp_path / "t" / "*.jsonl"))])
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "s.jsonl").write_text(_claude(2, "second"), encoding="utf-8")
    collect_once(collect)

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"day-log files: 1 corrupt line ({DAY}: 1); 1 torn last line closed" in out


# ------------------------------------------------------------ over-long records

MIB = 1024 * 1024


def _huge(minute: int, size: int, end: bool = True) -> str:
    """A user record of about *size* bytes: a pasted image is one base64 line."""
    line = _claude(minute, "look at this: " + "iVBORw0KGgo" * (size // 11))
    return line if end else line[:-1]


def _passes(cfg: DaylogConfig, n: int) -> list[object]:
    return [collect_once(cfg) for _ in range(n)]


def test_a_record_longer_than_one_read_is_skipped_and_the_turns_after_it_captured(
        tmp_path: Path) -> None:
    cfg, transcript = _source(tmp_path)
    _say(transcript, _claude(0, "before the image"), _huge(1, 9 * MIB),
         _claude(2, "after the image"), _claude(3, "and later still", who="assistant"))
    reports = _passes(cfg, 3)
    texts = [t.text for t in read_day_turns(cfg.day_jsonl(DAY))]
    assert texts == ["before the image", "after the image", "and later still"]
    skips = [s for r in reports for s in r.skipped]  # type: ignore[attr-defined]
    assert len(skips) == 1 and skips[0].path == str(transcript)
    start = len(_claude(0, "before the image").encode())
    assert skips[0].offset == start and skips[0].size == len(_huge(1, 9 * MIB).encode())
    fstate = DaylogState.load(cfg.state_path).sources["claude"].files[str(transcript)]
    assert fstate.offset == transcript.stat().st_size and fstate.waiting == 0
    assert fstate.parse_errors == 1 and [s[:2] for s in fstate.skipped] == [[start, skips[0].size]]
    assert DaylogState.load(cfg.state_path).sources["claude"].parse_errors == 1


def test_a_long_record_still_being_written_is_waited_for_not_skipped(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import smriti.daylog.collect as collect

    cfg, transcript = _source(tmp_path)
    _say(transcript, _claude(0, "before the image"), _huge(1, 9 * MIB, end=False))
    _passes(cfg, 2)
    start = len(_claude(0, "before the image").encode())
    fstate = DaylogState.load(cfg.state_path).sources["claude"].files[str(transcript)]
    assert fstate.offset == start and fstate.waiting == transcript.stat().st_size
    assert fstate.parse_errors == 0 and fstate.skipped == []

    looked: list[int] = []
    real = collect._next_line_start
    monkeypatch.setattr(collect, "_next_line_start",
                        lambda path, a, b: looked.append(a) or real(path, a, b))
    collect_once(cfg)
    assert looked == []  # nothing new: the bytes already looked at aren't read again
    _say(transcript, "\n", _claude(2, "after the image"))
    report = collect_once(cfg)
    assert looked == [fstate.waiting]  # only what came since
    assert len(report.skipped) == 1 and report.appended == 1
    assert [t.text for t in read_day_turns(cfg.day_jsonl(DAY))][-1] == "after the image"


def test_skipping_a_long_record_reads_it_a_step_at_a_time(tmp_path: Path) -> None:
    import tracemalloc

    cfg, transcript = _source(tmp_path)
    _say(transcript, _huge(1, 40 * MIB), _claude(2, "after"))
    tracemalloc.start()
    try:  # one read finds no newline; the pass looks for the end, skips, and reads on
        report = collect_once(cfg)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(report.skipped) == 1 and report.skipped[0].size > 40 * MIB
    assert peak < 12 * MIB, f"peak {peak / MIB:.1f} MiB"  # one read and one step, not 40 MiB
    assert [t.text for t in read_day_turns(cfg.day_jsonl(DAY))] == ["after"]


# ------------------------------------------------------------ a stuck source is visible


def _configured(tmp_path: Path) -> tuple[DaylogConfig, Path]:
    """A source configured in the memory root's daylog.json, as `smriti status` reads it."""
    from smriti.core.tree import tree_root
    from smriti.daylog.config import load_config

    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    root = tree_root()
    (root / ".smriti").mkdir(parents=True, exist_ok=True)
    (root / ".smriti" / "daylog.json").write_text(json.dumps({"sources": [
        {"name": "claude", "kind": "claude_jsonl", "glob": str(transcripts / "*.jsonl")}]}),
        encoding="utf-8")
    return load_config(root), transcripts / "5a3e0001.jsonl"


def _status(capsys: pytest.CaptureFixture[str]) -> str:
    from smriti.cli import main

    capsys.readouterr()
    assert main(["status"]) == 0
    return capsys.readouterr().out


def test_status_names_a_file_that_grows_while_capture_reads_none_of_it(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture) -> None:
    cfg, transcript = _configured(tmp_path)
    _say(transcript, _claude(0, "read, and long enough that the head fingerprint stays " * 5))
    collect_once(cfg)
    stuck_at = transcript.stat().st_size
    with caplog.at_level("WARNING", logger="smriti.daylog.collect"):
        for minute in (1, 2, 3):  # old Mac line endings: no line ever ends, as capture sees it
            _say(transcript, _claude(minute, "unread")[:-1] + "\r")
            collect_once(cfg)
    fstate = DaylogState.load(cfg.state_path).sources["claude"].files[str(transcript)]
    assert fstate.offset == stuck_at and fstate.stalled == 3
    assert sum("is stuck at byte" in r.getMessage() for r in caplog.records) == 1  # said once
    out = _status(capsys)
    assert (f"STUCK at byte {stuck_at:,} of {transcript.stat().st_size:,} in {transcript}: "
            "it grew in 3 passes") in out

    _say(transcript, "\n")  # the line ends at last: capture moves, and the file isn't stuck
    collect_once(cfg)
    assert DaylogState.load(cfg.state_path).sources["claude"].files[str(transcript)].stalled == 0
    assert "STUCK" not in _status(capsys)


def test_a_partial_line_that_stops_growing_is_not_stuck(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg, transcript = _configured(tmp_path)
    _say(transcript, _claude(0, "read"), _claude(1, "the writer died mid-line")[:30])
    _passes(cfg, 4)  # it grew once, then never again
    fstate = DaylogState.load(cfg.state_path).sources["claude"].files[str(transcript)]
    assert fstate.stalled < 2
    out = _status(capsys)
    assert "STUCK" not in out and "waiting" not in out and "skipped" not in out


def test_status_says_a_long_record_is_waited_for_and_then_skipped(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg, transcript = _configured(tmp_path)
    _say(transcript, _claude(0, "before"), _huge(1, 9 * MIB, end=False))
    _passes(cfg, 2)
    start = len(_claude(0, "before").encode())
    out = _status(capsys)
    assert (f"waiting for the end of a record longer than one read, at byte {start:,} of "
            f"{transcript} (") in out
    assert "STUCK" not in out  # the waiting line says why it doesn't move

    _say(transcript, "\n", _claude(2, "after"))
    collect_once(cfg)
    out = _status(capsys)
    assert "waiting" not in out
    assert "skipped 1 record longer than one read in 1 file; the last 9.0 MiB at byte " in out
    assert f"{start:,} of {transcript}" in out
    assert "1 parse errors" in out
