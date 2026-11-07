"""Codex rollouts in both encodings Codex has written (H12.17).

Codex CLI 0.163 (2026-10-22) writes the conversation as ``item_completed``
events (``UserMessage`` and ``AgentMessage`` items) and no longer the
``user_message`` and ``agent_message`` events the adapter read, so no
Codex turn was captured from it. The fixtures copy the structure of real
rollouts of each encoding, with made-up content (Sam, Ari, an orchard
ledger) and made-up ids, paths and times. Git may give them CRLF line
endings on Windows; they are copied with LF, as Codex writes them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smriti.daylog.adapters.codex_rollout import CodexRolloutAdapter
from smriti.daylog.collect import collect_once
from smriti.daylog.config import DaylogConfig, SourceSpec
from smriti.daylog.model import AGENT, USER
from smriti.daylog.state import DaylogState
from smriti.daylog.writer import read_whole_day

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"
PAGINATED = "rollout-2026-10-23T19-15-00-01a1aaaa-0b0b-7c0c-8d0d-0e0e0e0e0e01.jsonl"
LEGACY = "rollout-2026-07-15T23-06-13-01900000-0000-7000-8000-000000000001.jsonl"

FINAL_1 = ("The totals drift because rounding happens per row. I made it round once, "
           "at the end; the tests pass.")
FINAL_2 = "Yes: an empty ledger totals to zero, and there is a test for it now."

# What the adapter gave for the legacy fixture before H12.17 (run at fc27d39): a re-read
# of a file captured then must re-derive exactly these, or every turn would be appended twice.
LEGACY_TURNS = [
    (USER, "hi", "codex:orchard:01900000:f0c0fbd413e0", "2026-07-15T13:10:02.435000+00:00"),
    (AGENT, "Hi Sam, what shall we work on?", "codex:orchard:01900000:510f4d90dd30",
     "2026-07-15T13:10:15.273000+00:00"),
    (USER, "Let's tidy the ledger.", "codex:orchard:01900000:36b7b0c0527e",
     "2026-07-15T14:30:00+00:00"),
    (AGENT, "Done: the ledger rounds once now.", "codex:orchard:01900000:a8b0d14e18fe",
     "2026-07-15T14:33:55.323000+00:00"),
]


def _copy(name: str, dest: Path, as_name: str | None = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / (as_name or name)
    path.write_bytes((FIXTURES / name).read_bytes().replace(b"\r\n", b"\n"))
    return path


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8",
                    newline="\n")
    return path


def _extract(path: Path) -> list[tuple[str, str, str, str]]:
    result = CodexRolloutAdapter().extract(path, path.read_bytes())
    assert result.consumed == path.stat().st_size and result.errors == 0
    return [(t.who, t.text, t.id, t.ts.isoformat()) for t in result.turns]


def _paginated(record: dict[str, object]) -> dict[str, object]:
    """A legacy message event as Codex's migration rewrote it (same time, same text)."""
    payload = record.get("payload")
    if record.get("type") != "event_msg" or not isinstance(payload, dict):
        return record
    if payload.get("type") == "user_message":
        item: dict[str, object] = {"type": "UserMessage", "id": "u", "content": [
            {"type": "text", "text": payload["message"], "text_elements": []}]}
    elif payload.get("type") == "agent_message":
        item = {"type": "AgentMessage", "id": "a", "phase": payload.get("phase"),
                "content": [{"type": "Text", "text": payload["message"]}]}
    else:
        return record
    return {**record, "payload": {"type": "item_completed", "thread_id": "t", "turn_id": "t1",
                                  "item": item}}


# ------------------------------------------------------------ the adapter


def test_the_paginated_encoding_gives_the_conversation_and_nothing_else(tmp_path: Path) -> None:
    turns = _extract(_copy(PAGINATED, tmp_path))
    assert [(who, text) for who, text, _, _ in turns] == [
        (USER, "Ari, can you check why the orchard ledger totals drift?"),
        (AGENT, FINAL_1),  # not the commentary, the tool output, nor task_complete's copy
        (USER, "Thanks. Does it handle an empty ledger?"),  # its image block isn't text
        (AGENT, FINAL_2),
    ]
    assert {turn_id.rsplit(":", 1)[0] for _, _, turn_id, _ in turns} == {"codex:orchard:01a1aaaa"}
    assert [ts for *_, ts in turns] == [
        "2026-10-23T09:00:01+00:00", "2026-10-23T09:00:06+00:00",
        "2026-10-23T09:00:10+00:00", "2026-10-23T09:00:12+00:00"]


def test_the_legacy_encoding_gives_the_turns_and_ids_it_always_did(tmp_path: Path) -> None:
    assert _extract(_copy(LEGACY, tmp_path)) == LEGACY_TURNS


def test_a_message_has_the_same_id_in_either_encoding(tmp_path: Path) -> None:
    """Codex's migration kept each message's time and text, so its id doesn't change."""
    legacy = _copy(LEGACY, tmp_path / "a")
    (tmp_path / "b").mkdir()
    migrated = _write(tmp_path / "b" / LEGACY, [_paginated(r) for r in _records(legacy)])
    assert any(r["payload"].get("type") == "item_completed"  # type: ignore[attr-defined]
               for r in _records(migrated))
    assert _extract(migrated) == LEGACY_TURNS


def _mixed(tmp_path: Path) -> list[dict[str, object]]:
    """A thread begun in the legacy encoding and continued in the paginated one, with
    its first message ("hi") written in both, 0.5 s apart."""
    records = _records(_copy(LEGACY, tmp_path / "src"))
    head, tail = records[:4], records[4:]  # through "hi"; the rest of the thread
    both = {"timestamp": "2026-07-15T13:10:02.900Z", "type": "event_msg",
            "payload": {"type": "user_message", "message": "hi"}}
    return [*head, _paginated(both), *[_paginated(r) for r in tail]]


def test_a_mixed_file_gives_each_message_once(tmp_path: Path) -> None:
    assert _extract(_write(tmp_path / LEGACY, _mixed(tmp_path))) == LEGACY_TURNS


def test_a_mixed_file_gives_the_same_turns_wherever_a_read_ends(tmp_path: Path) -> None:
    """Codex review of H12.17: the two copies in different reads were both kept, with
    different ids, and the writer appended both for good."""
    path = _write(tmp_path / LEGACY, _mixed(tmp_path))
    data = path.read_bytes()
    ends = [i + 1 for i, byte in enumerate(data) if byte == ord("\n")][:-1]
    for end in ends:  # every line boundary, the one between the two copies included
        first = CodexRolloutAdapter().extract(path, data[:end])
        rest = CodexRolloutAdapter().extract(path, data[end:], first.carry)  # a new process
        got = [(t.who, t.text, t.id, t.ts.isoformat()) for t in first.turns + rest.turns]
        assert got == LEGACY_TURNS, f"read ended at byte {end}"


def test_capture_keeps_one_copy_when_the_two_arrive_in_separate_passes(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions" / "2026" / "07" / "15"
    sessions.mkdir(parents=True)
    records = _mixed(tmp_path)
    cfg = DaylogConfig(root=tmp_path / "root", sources=[_codex_source(tmp_path / "sessions")])
    path = _write(sessions / LEGACY, records[:4])  # through the legacy "hi"
    assert collect_once(cfg).appended == 1
    with path.open("a", encoding="utf-8", newline="\n") as f:  # its copy, and the rest
        f.write("".join(json.dumps(r) + "\n" for r in records[4:]))
    assert collect_once(cfg).appended == 3
    days = [cfg.day_jsonl(day) for day in ("2026-07-15", "2026-07-16")]
    lines = [line for day in days for line in day.read_text(encoding="utf-8").splitlines()]
    assert [json.loads(line)["id"] for line in lines] == [i for _, _, i, _ in LEGACY_TURNS]

    saved = cfg.state_path.read_text(encoding="utf-8")
    carry = json.loads(saved)["sources"]["codex"]["files"][str(path)]["carry"]
    assert carry and all(len(e) == 4 and len(e[1]) == 16 and int(e[1], 16) >= 0 for e in carry)
    assert not any(text in saved for _, text, _, _ in LEGACY_TURNS[1:])  # hashes, never text


def test_capture_gives_the_same_turns_however_short_its_reads(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import smriti.daylog.collect as collect

    sessions = tmp_path / "sessions" / "2026" / "07" / "15"
    sessions.mkdir(parents=True)
    path = _write(sessions / LEGACY, _mixed(tmp_path))
    longest = max(len(line) for line in path.read_bytes().splitlines(keepends=True))
    monkeypatch.setattr(collect, "_MAX_CHUNK", longest + 1)  # a read ends in every line
    cfg = DaylogConfig(root=tmp_path / "root", sources=[_codex_source(tmp_path / "sessions")])
    for _ in range(20):
        collect_once(cfg)
    assert DaylogState.load(cfg.state_path).sources["codex"].files[str(path)].offset == \
        path.stat().st_size
    turns = [*read_whole_day(cfg.day_jsonl("2026-07-15")),
             *read_whole_day(cfg.day_jsonl("2026-07-16"))]
    assert [t.id for t in turns] == [i for _, _, i, _ in LEGACY_TURNS]


def test_the_same_words_said_again_later_are_a_new_turn(tmp_path: Path) -> None:
    records = _records(_copy(LEGACY, tmp_path / "src"))
    again = {"timestamp": "2026-07-15T15:00:00.000Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "hi"}}
    path = _write(tmp_path / LEGACY, [*records, _paginated(again)])
    assert [text for _, text, _, _ in _extract(path)][-1] == "hi"
    assert len(_extract(path)) == len(LEGACY_TURNS) + 1


@pytest.mark.parametrize("cwd", ["C:\\Users\\sam\\.smriti\\.smriti\\llm-workdir",
                                 "/home/sam/.smriti/.smriti/llm-workdir"])
def test_a_session_in_smritis_own_llm_workdir_gives_no_turns(tmp_path: Path, cwd: str) -> None:
    """The nightly's `codex exec` fallback runs there; its rollout's path doesn't say so."""
    records = _records(_copy(PAGINATED, tmp_path / "src"))
    records[0]["payload"]["cwd"] = cwd  # type: ignore[index]
    path = _write(tmp_path / PAGINATED, records)
    result = CodexRolloutAdapter().extract(path, path.read_bytes())
    assert result.turns == [] and result.errors == 0 and result.consumed == path.stat().st_size


def test_a_message_without_text_is_skipped_but_a_broken_one_counted(tmp_path: Path) -> None:
    records = _records(_copy(PAGINATED, tmp_path / "src"))
    image_only = {"timestamp": "2026-10-23T09:01:00.000Z", "type": "event_msg", "payload": {
        "type": "item_completed", "item": {"type": "UserMessage", "content": [
            {"type": "image", "image_url": "data:image/png;base64,AAAA"}]}}}
    no_time = {"type": "event_msg", "payload": {"type": "item_completed", "item": {
        "type": "UserMessage", "content": [{"type": "text", "text": "when?"}]}}}
    path = _write(tmp_path / PAGINATED, [*records, image_only, no_time])
    result = CodexRolloutAdapter().extract(path, path.read_bytes())
    assert len(result.turns) == 4 and result.errors == 1


# ------------------------------------------------------------ capture and backfill


def _codex_source(sessions: Path) -> SourceSpec:
    return SourceSpec(name="codex", kind="codex_rollout",
                      glob=str(sessions / "*" / "*" / "*" / "rollout-*.jsonl"))


def test_capture_reads_both_encodings(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _copy(LEGACY, sessions / "2026" / "07" / "15")
    _copy(PAGINATED, sessions / "2026" / "10" / "23")
    cfg = DaylogConfig(root=tmp_path / "root", sources=[_codex_source(sessions)])
    assert collect_once(cfg).appended == 8
    assert [t.text for t in read_whole_day(cfg.day_jsonl("2026-10-23"))] == [
        "Ari, can you check why the orchard ledger totals drift?", FINAL_1,
        "Thanks. Does it handle an empty ledger?", FINAL_2]
    legacy = [*read_whole_day(cfg.day_jsonl("2026-07-15")),  # UTC+10: it crosses midnight
              *read_whole_day(cfg.day_jsonl("2026-07-16"))]
    assert [t.id for t in legacy] == [turn_id for _, _, turn_id, _ in LEGACY_TURNS]


def test_rescan_backfills_what_an_older_adapter_read_past(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The reference install's case: offsets at the end of rollouts the old adapter found
    no turn in. A pass reads nothing; a rescan appends their turns, and no other twice."""
    import smriti.daylog.adapters.codex_rollout as codex
    from smriti.cli import main
    from smriti.core.tree import tree_root

    sessions = tmp_path / "sessions"
    _copy(LEGACY, sessions / "2026" / "07" / "15")
    _copy(PAGINATED, sessions / "2026" / "10" / "23")
    root = tree_root()
    (root / ".smriti").mkdir(parents=True, exist_ok=True)
    (root / ".smriti" / "daylog.json").write_text(json.dumps({"sources": [
        {"name": "codex", "kind": "codex_rollout",
         "glob": str(sessions / "*" / "*" / "*" / "rollout-*.jsonl")}]}), encoding="utf-8")
    cfg = DaylogConfig(root=root, sources=[_codex_source(sessions)])

    real = codex._message
    with monkeypatch.context() as old_adapter:  # the adapter before H12.17
        old_adapter.setattr(codex, "_message", lambda p: None if p.get("type") ==
                            "item_completed" else real(p))
        assert collect_once(cfg).appended == 4
    assert collect_once(cfg).appended == 0  # its offsets are past those bytes
    assert not cfg.day_jsonl("2026-10-23").exists()

    assert main(["logd", "--rescan", "codex"]) == 0
    out = capsys.readouterr().out
    assert "Rescan: codex: 2 known files will be read again from the start" in out
    assert "Pass: +4 turns" in out and "days changed: 2026-10-23" in out
    assert len(read_whole_day(cfg.day_jsonl("2026-10-23"))) == 4
    legacy_lines = [line for day in ("2026-07-15", "2026-07-16")
                    for line in cfg.day_jsonl(day).read_text(encoding="utf-8").splitlines()]
    assert len(legacy_lines) == 4  # none twice
    state = DaylogState.load(cfg.state_path)
    assert all(f.offset == f.size for f in state.sources["codex"].files.values())

    assert main(["logd", "--rescan", "codex"]) == 0  # again: nothing new
    assert "Pass: +0 turns" in capsys.readouterr().out


def test_rescan_names_only_a_captured_source(capsys: pytest.CaptureFixture[str]) -> None:
    from smriti.cli import main

    assert main(["logd", "--rescan", "codex"]) == 1
    assert "No source named 'codex' is captured" in capsys.readouterr().err
