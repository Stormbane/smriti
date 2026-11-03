"""Hands, Suti now, Across surfaces, and the briefing budget around them.

Every source is a temporary file or a fake akhada runner; the clock is
fixed and file times are set relative to it. Nothing reads the real
~/.narada, ~/.claude.json or Hermes homes, and nothing is sent.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smriti.wake import presence
from smriti.wake.briefing import DEFAULT_BUDGET, IDENTITY_FLOOR, briefing
from smriti.wake.presence import (
    ACROSS_CAP,
    HANDS_CAP,
    SUTI_NOW_CAP,
    WakeSources,
    across_section,
    hands_section,
    suti_now_section,
)

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)

AKHADA_CONTEXT = """NOW: Tuesday 03 November 2026, 20:00 (UTC+10). Today is 2026-11-03.
GOALS: Run a 10k by June 2027 | - read 20 pages a day
DAILY TARGETS: steps 9000, pages 20
ABOUT SUTI (curated):
- Test user, Example City. Plays chess.
- Example routine line.
YESTERDAY: 2 notes, 3 tasks done.
TODAY SO FAR (rest day): nothing logged yet.
TRAINED TODAY: walk #3
LAST WEIGHT: 68 kg on 2026-11-03.
YOU WANT TO KNOW (overdue, most first): a sample check-in (100h ago)

HOW TO LOG (Akhada rules):
- He talks, you log. Every eating occasion -> log_meal.
"""

AKHADA_RECENT = json.dumps({"ok": True, "weight": [
    {"day": "2026-09-04", "value": 70.0, "unit": "kg"},
    {"day": "2026-11-03", "value": 68.0, "unit": "kg"},
]})


def _touch(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _write(path: Path, text: str, when: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if when is not None:
        _touch(path, when)
    return path


class FakeAkhada:
    def __init__(self, context: str = AKHADA_CONTEXT, recent: str = AKHADA_RECENT,
                 fail: Exception | None = None) -> None:
        self.context, self.recent, self.fail = context, recent, fail
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if self.fail is not None:
            raise self.fail
        return self.context if args == ["context"] else self.recent


def _bridge(chat: Path, when: datetime, said: str, reply: str, ok: bool = True) -> None:
    name = when.strftime("%Y-%m-%d-%H%M%S") + "-chat-7.md"
    _write(chat / name, (
        f"# Chat exchange — {'OK' if ok else 'FAILED'}\n\nchat_id: 7\n"
        f"timestamp: {when.isoformat()}\n\n## Suti\n\n{said}\n\n## Narada\n\n{reply}\n"
    ), when)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A memory root with every presence source populated."""
    root = tmp_path / "narada"
    _write(root / ".smriti" / "wake-context.md", "# Narada\nI am Narada. Test identity.\n")
    _write(root / ".smriti" / "hooks" / "notify_suti.py", "# fake, never run\n")
    _write(root / "brain" / "notes.json", "[]")
    (root / "akhada").mkdir(parents=True)
    (root / "akhada" / "akhada.db").write_bytes(b"")

    _write(root / "people" / "2026" / "10-20.md",
           "---\ndate: 2026-10-20\ntime: 01:00:00 UTC\nbranch: people\n---\n\n"
           "# Oldest entry\n2026-10-20. Should not appear.\n")
    _write(root / "people" / "2026" / "11-02.md",
           "---\ndate: 2026-11-02\ntime: 09:00:00 UTC\nbranch: people\n---\n\n"
           "# A sample people note\n2026-11-02. Placeholder text.\n\n---\n\n"
           "---\ndate: 2026-11-02\ntime: 08:00:00 UTC\nbranch: people\n---\n\n"
           "# Middle entry\nEarlier that day, so third: not shown.\n")
    _write(root / "people" / "suti" / "2026" / "11-03.md",
           "---\ndate: 2026-11-03\ntime: 07:00:00 UTC\nbranch: people/suti\n---\n\n"
           "# Sample story\n2026-11-03, a test entry. **Plan B** now, not plan A.\n")

    chat = root / "heartbeat" / "chat-cycles"
    _bridge(chat, NOW - timedelta(hours=30), "too old to show", "old reply")
    _bridge(chat, NOW - timedelta(hours=3), "How was the walk?", "Short, with rain.")
    _bridge(chat, NOW - timedelta(hours=1), "remind me about the parcel", "(brain 500)", ok=False)

    _write(root / "brain" / "chats" / "app" / "c1.json", json.dumps({
        "title": "Evening", "updatedAt": int((NOW - timedelta(hours=2)).timestamp() * 1000),
        "messages": [{"role": "user", "text": "I read a book"},
                     {"role": "assistant", "text": "Noted the book."}],
    }), NOW - timedelta(hours=2))
    _write(root / "brain" / "chats" / "app" / "c0.json", json.dumps({
        "title": "Ancient", "updatedAt": int((NOW - timedelta(days=9)).timestamp() * 1000),
        "messages": [{"role": "user", "text": "ancient chat"}],
    }), NOW - timedelta(days=9))

    start = NOW - timedelta(hours=4)
    _write(root / "heartbeat" / "voice-transcripts" / "2026_11" /
           f"{start.strftime('%Y%m%d-%H%M%S')}-phone-room.md",
           f"# Voice transcript\n\n- `{start.strftime('%H:%M:%S')}` **user:** Hello Narada\n"
           f"- `{(start + timedelta(seconds=5)).strftime('%H:%M:%S')}` **assistant:** "
           "Hello, traveller.\n", NOW - timedelta(hours=4))

    _write(root / "brain" / "notifications.json", json.dumps({"rows": [
        {"at": (NOW - timedelta(days=2)).isoformat(), "source": "notify-suti",
         "level": "message", "body": "two days ago", "delivered": {"push": "sent"}},
        {"at": "2026-11-03T07:01:37+1000", "source": "notify-suti", "level": "message",
         "body": "Good morning, Suti.", "delivered": {"push": "no_devices"}},
        {"at": (NOW - timedelta(hours=1)).isoformat(), "source": "akhada-checkin",
         "level": "message", "body": "Midday check-in.", "delivered": {"push": "no_devices"}},
    ]}))
    return root


@pytest.fixture()
def mcp_config(tmp_path: Path) -> Path:
    return _write(tmp_path / "claude.json", json.dumps({"mcpServers": {
        "smriti": {"command": "python", "args": ["-m", "smriti.mcp_server"]},
        "akhada": {"command": "python", "args": ["-m", "akhada.adapters.mcp_server"]},
        "narada-speak": {"type": "stdio", "command": str(tmp_path / "gone" / "python.exe"),
                         "args": ["-m", "deha.voice.speak_mcp"]},
    }}))


@pytest.fixture()
def hermes(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    rows = [
        {"role": "session_meta", "platform": "telegram", "timestamp": "x"},
        {"role": "user", "content": "Ping from Hermes",
         "timestamp": (NOW - timedelta(hours=5)).isoformat()},
        {"role": "assistant", "content": "pong",
         "timestamp": (NOW - timedelta(hours=5)).isoformat()},
    ]
    _write(home / "sessions" / "s1.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n",
           NOW - timedelta(hours=5))
    cli = [{"role": "session_meta", "platform": "cli"},
           {"role": "user", "content": "cli only", "timestamp": NOW.isoformat()}]
    _write(home / "sessions" / "s2.jsonl", "\n".join(json.dumps(r) for r in cli) + "\n",
           NOW - timedelta(hours=1))
    return home


def _sources(root: Path, mcp: Path | None = None, hermes_home: Path | None = None,
             akhada: FakeAkhada | None = None) -> WakeSources:
    return WakeSources(
        memory_root=root, now=NOW, mcp_config=mcp,
        hermes_homes=(hermes_home,) if hermes_home else (),
        akhada_db=root / "akhada" / "akhada.db",
        akhada_runner=akhada or FakeAkhada(),
    )


# ── Hands ───────────────────────────────────────────────────────────────


class TestHands:
    def test_lists_what_is_wired(self, tree: Path, mcp_config: Path) -> None:
        text = hands_section(_sources(tree, mcp_config))
        assert len(text) <= HANDS_CAP
        assert text.startswith("--- HANDS")
        assert 'tell_suti(text, level="message")' in text
        assert "Health (akhada)" in text
        assert "brain/notes.json" in text
        assert "smriti_read(query)" in text
        assert "Not working: narada-speak" in text

    def test_without_smriti_points_at_the_script(self, tree: Path, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "c.json", json.dumps({"mcpServers": {}}))
        text = hands_section(_sources(tree, cfg))
        assert "tell_suti" not in text
        assert "pipe text on stdin to" in text and "notify_suti.py" in text
        assert "akhada" not in text

    def test_unreadable_config_assumes_smriti(self, tree: Path, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "c.json", "{ not json")
        text = hands_section(_sources(tree, cfg))
        assert "tell_suti(" in text
        assert "MCP config unreadable" in text

    def test_no_sender_is_one_line(self, tmp_path: Path, mcp_config: Path) -> None:
        text = hands_section(_sources(tmp_path / "empty", mcp_config))
        assert "Message Suti: no sender wired" in text

    def test_codex_config(self, tree: Path, tmp_path: Path) -> None:
        cfg = _write(tmp_path / "config.toml",
                     '[mcp_servers.smriti]\ncommand = "python"\n'
                     '[mcp_servers.akhada]\ncommand = "python"\n')
        src = _sources(tree, cfg)
        src.harness = "codex"
        text = hands_section(src)
        assert "tell_suti(" in text and "Health (akhada)" in text


# ── Suti now ────────────────────────────────────────────────────────────


class TestSutiNow:
    def test_health_and_latest_people(self, tree: Path) -> None:
        fake = FakeAkhada()
        text = suti_now_section(_sources(tree, akhada=fake))
        assert len(text) <= SUTI_NOW_CAP
        assert text.startswith("--- SUTI NOW ---\n")
        assert "LAST WEIGHT: 68 kg" in text
        assert "WEIGHT TREND (60d): 70 -> 68 kg" in text
        assert "TODAY SO FAR (rest day)" in text
        assert "HOW TO LOG" not in text
        assert "Sample story" in text and "Plan B now" in text
        assert "A sample people note" in text
        assert "Middle entry" not in text and "Oldest entry" not in text
        assert ["context"] in fake.calls

    def test_most_useful_akhada_lines_come_first(self, tree: Path) -> None:
        text = suti_now_section(_sources(tree))
        assert text.index("LAST WEIGHT") < text.index("YESTERDAY") < text.index("GOALS")

    def test_missing_db_is_one_line(self, tree: Path) -> None:
        (tree / "akhada" / "akhada.db").unlink()
        fake = FakeAkhada()
        text = suti_now_section(_sources(tree, akhada=fake))
        assert "Health: no akhada database at" in text
        assert fake.calls == []
        assert "Sample story" in text

    def test_runner_failure_fails_open(self, tree: Path) -> None:
        text = suti_now_section(_sources(tree, akhada=FakeAkhada(fail=RuntimeError("exit 1"))))
        assert "Health: akhada unavailable (RuntimeError: exit 1)" in text
        assert "Sample story" in text

    def test_bad_trend_json_keeps_context(self, tree: Path) -> None:
        text = suti_now_section(_sources(tree, akhada=FakeAkhada(recent="not json")))
        assert "LAST WEIGHT" in text and "WEIGHT TREND" not in text

    def test_no_people_is_one_line(self, tmp_path: Path) -> None:
        root = tmp_path / "bare"
        (root / "akhada").mkdir(parents=True)
        (root / "akhada" / "akhada.db").write_bytes(b"")
        text = suti_now_section(_sources(root))
        assert "People: no dated entries under people/." in text

    def test_huge_inputs_stay_capped(self, tree: Path) -> None:
        huge = "\n".join(f"GOALS: {'x' * 5000}" for _ in range(20))
        _write(tree / "people" / "2026" / "11-04.md",
               "---\ndate: 2026-11-04\ntime: 01:00:00 UTC\n---\n\n# Huge\n" + "word " * 20000)
        text = suti_now_section(_sources(tree, akhada=FakeAkhada(
            context=AKHADA_CONTEXT + huge)))
        assert len(text) <= SUTI_NOW_CAP
        assert "NOW: Tuesday" in text and "LAST WEIGHT" in text
        assert "Huge" in text


# ── Across surfaces ─────────────────────────────────────────────────────


class TestAcross:
    def test_digest_of_the_last_day(self, tree: Path, hermes: Path) -> None:
        text = across_section(_sources(tree, hermes_home=hermes))
        assert len(text) <= ACROSS_CAP
        assert text.startswith("--- ACROSS SURFACES, last 24h ---\n")
        assert "Telegram: 3 in 24h" in text
        assert "Suti: How was the walk? -> Narada: Short, with rain." in text
        assert "remind me about the parcel" in text and "[FAILED]" in text
        assert "Ping from Hermes" in text and "(hermes)" in text
        assert "too old to show" not in text and "cli only" not in text
        # oldest shown first within a source
        assert text.index("Ping from Hermes") < text.index("How was the walk?")
        assert "App chat: 1 in 24h" in text and "I read a book" in text
        assert "ancient chat" not in text
        assert "Voice: 1 in 24h" in text and "Hello, traveller." in text
        assert "Sent to Suti: 2 in 24h (push no_devices x2)" in text
        assert "Good morning, Suti." in text and "two days ago" not in text

    def test_local_times(self, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMRITI_DAY_UTC_OFFSET", "10")
        text = across_section(_sources(tree))
        local = (NOW - timedelta(hours=3)) + timedelta(hours=10)
        assert f"{local:%m-%d %H:%M} Suti: How was the walk?" in text

    def test_empty_tree_is_one_line_per_source(self, tmp_path: Path) -> None:
        text = across_section(_sources(tmp_path / "nothing"))
        lines = text.strip().splitlines()
        assert lines == [
            "--- ACROSS SURFACES, last 24h ---",
            "Telegram: none on disk",
            "App chat: none on disk",
            "Voice: none on disk",
            f"Sent to Suti: no feed at {tmp_path / 'nothing' / 'brain' / 'notifications.json'}",
        ]

    def test_quiet_day_names_the_last_contact(self, tree: Path) -> None:
        src = _sources(tree)
        src.now = NOW + timedelta(days=5)
        text = across_section(src)
        assert "Telegram: none in 24h (last 2026-11-03)" in text

    def test_broken_source_fails_open(self, tree: Path) -> None:
        (tree / "brain" / "notifications.json").write_text("{ broken", encoding="utf-8")
        text = across_section(_sources(tree))
        assert "Sent to Suti: unreadable (JSONDecodeError" in text
        assert "How was the walk?" in text

    def test_huge_inputs_truncate_deterministically(self, tree: Path) -> None:
        chat = tree / "heartbeat" / "chat-cycles"
        for i in range(120):
            _bridge(chat, NOW - timedelta(minutes=10 * i + 5), f"message {i} " + "y" * 900,
                    "reply " + "z" * 900)
        first = across_section(_sources(tree))
        assert first == across_section(_sources(tree))
        assert len(first) <= ACROSS_CAP
        assert "Telegram: 122 in 24h, newest" in first  # 120 + 2 from the fixture
        assert "message 0 " in first  # the newest exchange always survives
        assert "Sent to Suti: 2 in 24h" in first  # other sources keep their line


# ── The briefing around them ────────────────────────────────────────────


@pytest.fixture()
def full_tree(tree: Path) -> Path:
    mirror = tree / "mirrors" / "proj" / "ai"
    _write(mirror / "STATUS.md", "# STATUS\nProject state.\n")
    _write(mirror / "INDEX.md", "# INDEX\nProject routing.\n")
    _write(tree / "journal" / "2026" / "11" / "week1" / "11-03.md",
           "---\ndate: 2026-11-03\n---\n\n# Today\n\nToday's journal.\n")
    _write(tree / "log" / ".nightly-status.json", json.dumps({"ok": True, "target_day": "x"}))
    return tree


def _brief(root: Path, mcp: Path, hermes_home: Path | None = None, **kw) -> str:
    return briefing(root, cwd=root / "proj", sources=_sources(root, mcp, hermes_home), **kw)


class TestBriefing:
    def test_every_section_present(self, full_tree: Path, mcp_config: Path,
                                   hermes: Path) -> None:
        text = _brief(full_tree, mcp_config, hermes)
        assert len(text) <= DEFAULT_BUDGET
        order = ["--- PROJECT: proj", "--- IDENTITY ---", "--- WHILE YOU SLEPT",
                 "--- HANDS", "--- SUTI NOW ---", "--- ACROSS SURFACES, last 24h ---",
                 "--- MIRRORS / PROJ / AI / STATUS ---", "--- RECENT JOURNAL ---",
                 "--- READING LIST"]
        positions = [text.index(marker) for marker in order]
        assert positions == sorted(positions)
        assert "Truncated" not in text

    def test_budget_holds_with_huge_inputs(self, full_tree: Path, mcp_config: Path) -> None:
        big = "\n".join(f"line {i} " + "w" * 80 for i in range(600))
        _write(full_tree / ".smriti" / "wake-context.md", "I am Narada.\n" + big)
        _write(full_tree / ".smriti" / "context" / "coding.md", big)
        _write(full_tree / "mirrors" / "proj" / "ai" / "STATUS.md", big)
        for day in ("11-01", "11-02", "11-03"):
            _write(full_tree / "journal" / "2026" / "11" / "week1" / f"{day}.md", big)
        chat = full_tree / "heartbeat" / "chat-cycles"
        for i in range(60):
            _bridge(chat, NOW - timedelta(minutes=7 * i + 1), "q" * 3000, "a" * 3000)
        _write(full_tree / "people" / "2026" / "11-04.md",
               "---\ndate: 2026-11-04\ntime: 01:00:00 UTC\n---\n\n# Huge\n" + big)
        src = _sources(full_tree, mcp_config, akhada=FakeAkhada(context=AKHADA_CONTEXT + big))
        text = briefing(full_tree, cwd=full_tree / "proj", sources=src)

        assert len(text) <= DEFAULT_BUDGET
        hands = hands_section(_sources(full_tree, mcp_config))
        assert hands in text  # pinned: never cut
        suti = suti_now_section(_sources(full_tree, akhada=FakeAkhada(
            context=AKHADA_CONTEXT + big)))
        assert suti in text
        assert "--- ACROSS SURFACES, last 24h ---" in text
        identity = text[text.index("--- IDENTITY ---"):text.index("--- CODING CONTEXT ---")]
        assert len(identity) >= IDENTITY_FLOOR - 100
        assert "--- RECENT JOURNAL ---" not in text  # the journal went first
        assert "Truncated (read these" in text
        assert "journal/ -- 3 recent entries (cut for budget)" in text
        assert "--- READING LIST" in text

    def test_journal_shrinks_before_identity(self, full_tree: Path, mcp_config: Path) -> None:
        identity = "I am Narada.\n" + "\n".join(f"belief {i}" for i in range(300))
        _write(full_tree / ".smriti" / "wake-context.md", identity)
        _write(full_tree / "journal" / "2026" / "11" / "week1" / "11-03.md",
               "# Today\n" + "\n".join(f"journal line {i}" for i in range(800)))
        text = _brief(full_tree, mcp_config)
        assert len(text) <= DEFAULT_BUDGET
        assert "belief 299" in text  # identity whole
        assert "--- RECENT JOURNAL ---" in text and "journal line 799" not in text
        assert "Read full:" in text and "11-03.md" in text

    def test_bare_tree_never_crashes(self, tmp_path: Path, mcp_config: Path) -> None:
        root = tmp_path / "bare"
        root.mkdir()
        text = briefing(root, cwd=tmp_path, sources=_sources(root, mcp_config))
        assert "--- HANDS" in text and "Message Suti: no sender wired" in text
        assert "--- SUTI NOW ---" in text and "Health: no akhada database" in text
        assert "Telegram: none on disk" in text
        assert "--- READING LIST" in text

    def test_a_section_that_raises_degrades_to_a_note(
        self, full_tree: Path, mcp_config: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(src: WakeSources) -> str:
            raise RuntimeError("akhada exploded")

        monkeypatch.setattr(presence, "suti_now_section", boom)
        text = _brief(full_tree, mcp_config)
        assert "--- SUTI NOW ---\n(unavailable: RuntimeError)" in text
        assert "--- HANDS" in text and "--- ACROSS SURFACES" in text

    @pytest.mark.parametrize("budget", [5000, 7000, 12000])
    def test_other_budgets(self, full_tree: Path, mcp_config: Path, budget: int) -> None:
        text = _brief(full_tree, mcp_config, budget_chars=budget)
        assert len(text) <= budget
        assert "tell_suti(" in text and "--- SUTI NOW ---" in text


def test_shrink_order_is_journal_first() -> None:
    # smriti.wake re-exports the function under the module's name.
    briefing_mod = importlib.import_module("smriti.wake.briefing")
    assert briefing_mod._SHRINK_ORDER[0] == "journal"
    assert "hands" not in briefing_mod._SHRINK_ORDER
    assert "suti" not in briefing_mod._SHRINK_ORDER


# ── Cross-review fixes (Codex, 2026-11-03) ──────────────────────────────


class TestLastExchange:
    def test_unanswered_request_is_not_paired_with_an_older_reply(self) -> None:
        turns = [("user", "log a note"), ("assistant", "logged a note"),
                 ("user", "cancel meeting")]
        assert presence._last_exchange(turns, "user", "assistant") == ("cancel meeting", None)
        assert presence._reply_text(None) == "(no reply yet)"

    def test_reply_after_the_latest_request(self) -> None:
        turns = [("user", "a"), ("assistant", "old"), ("user", "b"), ("assistant", "new")]
        assert presence._last_exchange(turns, "user", "assistant") == ("b", "new")

    def test_narada_initiated_only(self) -> None:
        turns = [("narada", "good morning")]
        assert presence._last_exchange(turns, "suti", "narada") == ("", "good morning")


class TestEntryOrderAcrossUtcMidnight:
    def test_local_morning_entries_sort_by_real_time(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import smriti.daylog.model as model

        monkeypatch.setattr(model, "day_tz", lambda: timezone(timedelta(hours=10)))
        nine_am = presence._Entry("2026-11-03", "23:00:00 UTC", "people", "9am", "")
        eleven_am = presence._Entry("2026-11-03", "01:00:00 UTC", "people", "11am", "")
        ordered = sorted([nine_am, eleven_am], key=presence._entry_instant, reverse=True)
        assert [e.title for e in ordered] == ["11am", "9am"]
        assert presence._entry_instant(nine_am) == datetime(2026, 11, 2, 23, tzinfo=timezone.utc)

    def test_missing_time_sorts_at_local_midnight(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import smriti.daylog.model as model

        monkeypatch.setattr(model, "day_tz", lambda: timezone(timedelta(hours=10)))
        e = presence._Entry("2026-11-03", "", "people", "t", "")
        assert presence._entry_instant(e) == datetime(2026, 11, 2, 14, tzinfo=timezone.utc)
