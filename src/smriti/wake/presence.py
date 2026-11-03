"""Wake sections about reach and the present moment.

Three sections join the identity briefing:

- **Hands**: what this session can do and how, generated from what is
  configured (MCP servers, the notify sender, the notes file).
- **Suti now**: akhada's health context plus the latest ``people/``
  entries.
- **Across surfaces, last 24h**: Telegram exchanges, app chats, voice
  turns and notifications sent.

Everything here is a deterministic read of local files or a local CLI.
There is no model call and no network. Each reader fails open: a
missing or broken source becomes a one-line note and never breaks the
wake. Every section is capped by its builder, so the briefing's budget
arithmetic can rely on the caps.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HANDS_CAP = 600
SUTI_NOW_CAP = 1800
ACROSS_CAP = 1500

# Inside "Suti now": akhada first, then people/ gets the rest.
AKHADA_CAP = 1000
PEOPLE_ENTRIES = 2
AKHADA_TIMEOUT_S = 5.0

WINDOW = timedelta(hours=24)

# (cli args after `python -m akhada --db <db>`) -> stdout
AkhadaRunner = Callable[[list[str]], str]


# ── sources ─────────────────────────────────────────────────────────────


@dataclass
class WakeSources:
    """Where the presence sections read from. ``for_root`` fills in the
    real locations; tests build one over a temporary tree."""

    memory_root: Path
    now: datetime
    mcp_config: Path | None = None
    harness: str = "claude"  # "claude" (~/.claude.json) | "codex" (config.toml)
    cwd: Path | None = None
    hermes_homes: tuple[Path, ...] = ()
    akhada_db: Path | None = None
    akhada_runner: AkhadaRunner | None = None
    window: timedelta = WINDOW
    _servers: dict[str, dict[str, Any]] | None = field(default=None, repr=False)
    _servers_loaded: bool = field(default=False, repr=False)

    @classmethod
    def for_root(
        cls,
        memory_root: Path,
        *,
        cwd: Path | None = None,
        now: datetime | None = None,
        harness: str = "claude",
    ) -> WakeSources:
        home = Path.home()
        mcp_config = (
            home / ".codex" / "config.toml" if harness == "codex" else home / ".claude.json"
        )
        hermes: list[Path] = []
        for raw in (
            os.environ.get("HERMES_HOME"),
            str(Path(os.environ["LOCALAPPDATA"]) / "hermes") if os.environ.get(
                "LOCALAPPDATA") else None,
            str(home / ".hermes"),
        ):
            if raw and Path(raw) not in hermes:
                hermes.append(Path(raw))
        db_env = os.environ.get("AKHADA_DB")
        akhada_db = Path(db_env).expanduser() if db_env else memory_root / "akhada" / "akhada.db"
        return cls(
            memory_root=memory_root,
            now=now or datetime.now(timezone.utc),
            mcp_config=mcp_config,
            harness=harness,
            cwd=cwd,
            hermes_homes=tuple(hermes),
            akhada_db=akhada_db,
        )

    @property
    def since(self) -> datetime:
        return self.now - self.window

    def mcp_servers(self) -> dict[str, dict[str, Any]] | None:
        """Configured MCP servers by name; None when the config can't be read."""
        if not self._servers_loaded:
            self._servers_loaded = True
            try:
                self._servers = _load_mcp_servers(self.mcp_config, self.harness, self.cwd)
            except Exception:  # noqa: BLE001 — fail open
                self._servers = None
        return self._servers

    def run_akhada(self, args: list[str]) -> str:
        runner = self.akhada_runner or _subprocess_akhada_runner(
            _akhada_python(self.mcp_servers()), self.akhada_db or Path("akhada.db"))
        return runner(args)


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()


def _load_mcp_servers(
    path: Path | None, harness: str, cwd: Path | None,
) -> dict[str, dict[str, Any]] | None:
    if path is None or not path.is_file():
        return None
    if harness == "codex":
        import tomllib

        toml = tomllib.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in (toml.get("mcp_servers") or {}).items() if isinstance(v, dict)}
    data = json.loads(path.read_text(encoding="utf-8"))
    servers: dict[str, dict[str, Any]] = {
        k: v for k, v in (data.get("mcpServers") or {}).items() if isinstance(v, dict)
    }
    if cwd is not None:
        want = _norm(str(cwd))
        for key, project in (data.get("projects") or {}).items():
            if _norm(key) == want and isinstance(project, dict):
                servers.update({
                    k: v for k, v in (project.get("mcpServers") or {}).items()
                    if isinstance(v, dict)
                })
    return servers


def _akhada_python(servers: dict[str, dict[str, Any]] | None) -> str:
    """The interpreter akhada's MCP server runs under, else this one."""
    spec = (servers or {}).get("akhada") or {}
    cmd = str(spec.get("command") or "")
    if cmd and Path(cmd).name.lower().startswith("python"):
        resolved = cmd if Path(cmd).is_absolute() else shutil.which(cmd)
        if resolved:
            return resolved
    return sys.executable


def _subprocess_akhada_runner(python: str, db: Path) -> AkhadaRunner:
    def run(args: list[str]) -> str:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.run(
            [python, "-m", "akhada", "--db", str(db), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=AKHADA_TIMEOUT_S, stdin=subprocess.DEVNULL, creationflags=flags,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            raise RuntimeError(f"exit {proc.returncode}: {tail[0][:120]}")
        return proc.stdout

    return run


# ── text helpers ────────────────────────────────────────────────────────


def _clip(text: str, n: int) -> str:
    """Whitespace-collapsed, cut at a word boundary, with an ellipsis."""
    flat = " ".join(str(text).split())
    if len(flat) <= n:
        return flat
    if n <= 1:
        return flat[:max(n, 0)]
    cut = flat[:n - 1]
    space = cut.rfind(" ")
    if space > n // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def _fit_lines(lines: list[str], cap: int) -> str:
    """Join lines under *cap* chars (newlines included). A line that
    would overflow is clipped if there is room for a useful stub,
    and the rest are dropped. Always ends with a newline."""
    out: list[str] = []
    used = 0
    for line in lines:
        need = len(line) + 1
        if used + need <= cap:
            out.append(line)
            used += need
            continue
        room = cap - used - 1
        if room >= 40:
            out.append(_clip(line, room))
        break
    return "\n".join(out) + "\n" if out else ""


def _local(ts: datetime) -> datetime:
    from smriti.daylog.model import day_tz

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(day_tz())


def _stamp(ts: datetime) -> str:
    return _local(ts).strftime("%m-%d %H:%M")


def _day(ts: datetime) -> str:
    return _local(ts).strftime("%Y-%m-%d")


def _why(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_clip(str(exc), 80)}" if str(exc) else type(exc).__name__


# ── Hands ───────────────────────────────────────────────────────────────

_NOTES_SERVERS = ("narada-notes", "notes")


def _broken_server(name: str, spec: dict[str, Any]) -> str | None:
    if spec.get("url") or spec.get("type") in ("http", "sse"):
        return None
    cmd = str(spec.get("command") or "")
    if not cmd:
        return None
    if Path(cmd).is_absolute() or "/" in cmd or "\\" in cmd:
        return None if Path(cmd).exists() else f"{name} ({_clip(cmd, 60)} missing)"
    return None if shutil.which(cmd) else f"{name} ({cmd} not on PATH)"


def hands_section(src: WakeSources) -> str:
    """What this session can do and how, from what is configured."""
    from smriti.daylog.config import load_config
    from smriti.daylog.notify import notify_command

    servers = src.mcp_servers()

    def has(name: str) -> bool:
        # An unreadable config: assume smriti, which is serving this wake.
        return name in servers if servers is not None else name == "smriti"

    lines = ["--- HANDS (what I can do from here) ---"]
    try:
        sender = notify_command(load_config(src.memory_root))
    except Exception:  # noqa: BLE001
        sender = []
    if sender and has("smriti"):
        lines.append(
            '- Message Suti: tell_suti(text, level="message") reaches Telegram and the '
            'app; it says which channels delivered. Faults: level="error". '
            "Confirm misheard names first."
        )
    elif sender:
        lines.append(
            f"- Message Suti: pipe text on stdin to {' '.join(sender)} "
            "(env NOTIFY_LEVEL=message)."
        )
    else:
        lines.append("- Message Suti: no sender wired (no notify_cmd, no notify_suti.py).")
    if has("akhada"):
        lines.append(
            "- Health (akhada): log_meal, log_vital, log_workout, set_day_kind; "
            "get_today to read. He talks, you log."
        )
    notes = src.memory_root / "brain" / "notes.json"
    if any(has(n) for n in _NOTES_SERVERS):
        lines.append("- Notes (his lists): notes_list, notes_get, and the notes edit tools.")
    elif notes.is_file():
        lines.append("- Notes (his lists): read brain/notes.json in the memory tree; no notes tool.")
    if has("smriti"):
        lines.append(
            "- Memory: smriti_read(query) before guessing; smriti_write(content, branch); "
            "smriti_ask_suti queues a question."
        )
    if servers is not None:
        broken = [b for n, s in sorted(servers.items()) if (b := _broken_server(n, s))]
        if broken:
            lines.append("- Not working: " + "; ".join(broken) + ".")
    else:
        lines.append("- (MCP config unreadable; tools listed from defaults.)")
    return _fit_lines(lines, HANDS_CAP)


# ── Suti now ────────────────────────────────────────────────────────────

# A new akhada context block starts at an upper-case label, e.g.
# "LAST WEIGHT:", "YESTERDAY (fast day):", "ABOUT SUTI (curated):".
_AKHADA_LABEL_RE = re.compile(r"^([A-Z][A-Z' ]+?)(?:\s*\([^)]*\))?:")

# Lower = kept longer. The logging rules are dropped outright: Hands
# already says how to log, and get_context carries them.
_AKHADA_PRIORITY = {
    "NOW": 0, "LAST WEIGHT": 0, "WEIGHT TREND": 0, "TODAY SO FAR": 0,
    "TRAINED TODAY": 0, "YOU WANT TO KNOW": 1, "YESTERDAY": 1,
    "GOALS": 2, "DAILY TARGETS": 2, "ABOUT SUTI": 3,
}
_AKHADA_DROP = {"HOW TO LOG"}
_AKHADA_LINE_MAX = 260


def _akhada_blocks(raw: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, list[str]]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = _AKHADA_LABEL_RE.match(line)
        if m:
            blocks.append((m.group(1).strip(), [line.strip()]))
        elif blocks:
            blocks[-1][1].append(line.strip())
    return [(key, " ".join(parts)) for key, parts in blocks if key not in _AKHADA_DROP]


def _weight_trend(raw: str) -> str | None:
    data = json.loads(raw)
    rows = [r for r in data.get("weight") or [] if isinstance(r, dict) and "value" in r]
    if len(rows) < 2:
        return None
    first, last = rows[0], rows[-1]
    unit = last.get("unit") or "kg"
    return (f"WEIGHT TREND (60d): {first['value']:g} -> {last['value']:g} {unit} "
            f"({first.get('day', '?')} -> {last.get('day', '?')}, {len(rows)} readings).")


def _akhada_lines(src: WakeSources) -> list[str]:
    db = src.akhada_db
    if db is None or not db.is_file():
        return [f"Health: no akhada database at {db}."]
    try:
        raw = src.run_akhada(["context"])
    except Exception as exc:  # noqa: BLE001 — fail open
        return [f"Health: akhada unavailable ({_why(exc)}); try get_context."]
    blocks = _akhada_blocks(raw)
    if not blocks:
        return ["Health: akhada returned no context; try get_context."]
    try:
        trend = _weight_trend(src.run_akhada(["call", "get_recent", '{"days": 60}']))
    except Exception:  # noqa: BLE001 — the trend is a nicety
        trend = None
    if trend:
        at = next((i for i, (k, _) in enumerate(blocks) if k == "LAST WEIGHT"), len(blocks))
        blocks.insert(at + 1, ("WEIGHT TREND", trend))
    # Most useful first (stable within a priority), then drop from the end.
    blocks.sort(key=lambda b: _AKHADA_PRIORITY.get(b[0], 2))
    kept = [(key, _clip(text, _AKHADA_LINE_MAX)) for key, text in blocks]
    header = "Health (akhada; get_context for the full picture):"

    def size(items: list[tuple[str, str]]) -> int:
        return len(header) + 1 + sum(len(t) + 1 for _, t in items)

    while size(kept) > AKHADA_CAP and len(kept) > 1:
        worst = max(range(len(kept)), key=lambda i: (_AKHADA_PRIORITY.get(kept[i][0], 2), i))
        kept.pop(worst)
    return [header, *(text for _, text in kept)]


_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(?=date:)", re.MULTILINE)
_DATED_FILE_RE = re.compile(r"^(\d{2})-(\d{2})(?:-\d+)?\.md$")
_LEADING_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[.,:;]?\s*")


@dataclass
class _Entry:
    date: str
    time: str
    branch: str
    title: str
    body: str


def _parse_entries(text: str, fallback_date: str, branch: str) -> list[_Entry]:
    entries: list[_Entry] = []
    starts = [m.start() for m in _FRONTMATTER_RE.finditer(text)] or [0]
    for i, start in enumerate(starts):
        chunk = text[start:starts[i + 1] if i + 1 < len(starts) else len(text)]
        meta: dict[str, str] = {}
        body = chunk
        if chunk.startswith("---"):
            lines = chunk.splitlines()
            end = next((j for j in range(1, len(lines)) if lines[j].strip() == "---"), None)
            if end is not None:
                for line in lines[1:end]:
                    key, _, value = line.partition(":")
                    meta[key.strip()] = value.strip()
                body = "\n".join(lines[end + 1:])
        title = ""
        rest: list[str] = []
        for line in body.splitlines():
            stripped = line.strip()
            if not title and stripped.startswith("# "):
                title = stripped[2:].strip()
            elif stripped != "---":
                rest.append(stripped)
        text_body = " ".join(" ".join(rest).replace("**", "").split())
        text_body = _LEADING_DATE_RE.sub("", text_body)
        if not (title or text_body):
            continue
        entries.append(_Entry(
            date=meta.get("date") or fallback_date,
            time=meta.get("time") or "",
            branch=meta.get("branch") or branch,
            title=title,
            body=text_body,
        ))
    return entries


def _entry_instant(e: _Entry) -> datetime:
    """When an entry was written, as an aware UTC datetime.

    write_entry() stamps the *machine-local* calendar date with a *UTC*
    clock ("date: 2026-11-03", "time: 23:00:00 UTC"), so the strings don't
    sort together across UTC midnight. Find the UTC instant whose
    machine-local date is the stamped date, using the writer's own
    convention (``datetime.astimezone()``), not the day-log offset.
    Entries without a usable time sort at local midnight of their date.
    """
    try:
        day = datetime.strptime(e.date, "%Y-%m-%d")
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    m = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", e.time or "")
    if not m:
        return _writer_midnight(day)
    clock = day.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                        second=int(m.group(3) or 0), tzinfo=timezone.utc)
    for shift in (0, -1, 1):
        candidate = clock + timedelta(days=shift)
        if _writer_local(candidate).date() == day.date():
            return candidate
    return clock


def _writer_local(ts: datetime) -> datetime:
    """An instant in the timezone write_entry() used (the machine's)."""
    return ts.astimezone()


def _writer_midnight(day: datetime) -> datetime:
    """Midnight of a naive calendar day in the writer's timezone, as UTC."""
    return day.astimezone(timezone.utc)  # a naive datetime is machine-local


def _latest_people(root: Path, n: int) -> list[_Entry]:
    people = root / "people"
    files: list[tuple[str, Path]] = []
    for path in people.rglob("*.md"):
        m = _DATED_FILE_RE.match(path.name)
        if m and re.fullmatch(r"\d{4}", path.parent.name):
            files.append((f"{path.parent.name}-{m.group(1)}-{m.group(2)}", path))
    files.sort(key=lambda f: (f[0], str(f[1])), reverse=True)
    entries: list[_Entry] = []
    for i, (day, path) in enumerate(files):
        if len(entries) >= n and day < entries[n - 1].date:
            break  # older files can't beat what we already hold
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        branch = path.parent.parent.relative_to(root).as_posix()
        entries.extend(_parse_entries(text, day, branch))
        entries.sort(key=_entry_instant, reverse=True)
        if i > 50:
            break
    return entries[:n]


def _people_lines(src: WakeSources, room: int) -> list[str]:
    try:
        entries = _latest_people(src.memory_root, PEOPLE_ENTRIES)
    except Exception as exc:  # noqa: BLE001 — fail open
        return [f"People: unreadable ({_why(exc)})."]
    if not entries:
        return ["People: no dated entries under people/."]
    lines = ["Lately (people/):"]
    each = max(80, (room - len(lines[0]) - 1) // len(entries) - 1)
    for e in entries:
        head = f"- {e.date} [{e.branch}] {e.title}".rstrip()
        lines.append(_clip(f"{head}: {e.body}" if e.body else head, each))
    return lines


def suti_now_section(src: WakeSources) -> str:
    """Health context from akhada plus the latest people/ entries."""
    header = "--- SUTI NOW ---"
    try:
        health = _akhada_lines(src)
    except Exception as exc:  # noqa: BLE001 — fail open
        health = [f"Health: unavailable ({_why(exc)})."]
    health_text = _fit_lines(health, AKHADA_CAP)
    room = SUTI_NOW_CAP - len(header) - 1 - len(health_text)
    people = _fit_lines(_people_lines(src, room), room)
    return header + "\n" + health_text + people


# ── Across surfaces ─────────────────────────────────────────────────────


@dataclass
class _Item:
    ts: datetime
    text: str


@dataclass
class _Group:
    title: str
    items: list[_Item] = field(default_factory=list)  # any order; sorted on render
    note: str = ""  # shown when there are no items
    summary: str = ""  # appended to the header when there are items


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.astimezone()  # naive = this machine's local time


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def _none_since(last: datetime | None) -> str:
    return f"none in 24h (last {_day(last)})" if last else "none on disk"


_BRIDGE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})-chat-")


def _bridge_exchange(path: Path) -> _Item | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = _BRIDGE_NAME_RE.match(path.name)
    ts = None
    for line in text.splitlines():
        if line.startswith("timestamp:"):
            ts = _parse_iso(line.partition(":")[2])
            break
    if ts is None and m:
        ts = datetime.strptime(m.group(1), "%Y-%m-%d-%H%M%S").replace(tzinfo=timezone.utc)
    if ts is None:
        return None
    suti, _, narada = text.partition("## Narada")
    suti = suti.partition("## Suti")[2]
    failed = "FAILED" in text.splitlines()[0] if text else False
    said = f"Suti: {_clip(suti, 220)} -> Narada: {_clip(narada, 140)}"
    return _Item(ts, said + (" [FAILED]" if failed else ""))


def _hermes_items(src: WakeSources) -> tuple[list[_Item], datetime | None]:
    items: list[_Item] = []
    last: datetime | None = None
    for home in src.hermes_homes:
        sessions = home / "sessions"
        if not sessions.is_dir():
            continue
        for path in sessions.glob("*.jsonl"):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            rows = []
            if mtime >= src.since:
                rows = [json.loads(line) for line in path.read_text(
                    encoding="utf-8", errors="replace").splitlines() if line.strip()]
            elif last is None or mtime > last:
                # Out of the window: only the date matters, unless it's Telegram.
                with path.open(encoding="utf-8", errors="replace") as fh:
                    head = json.loads(fh.readline() or "{}")
                if head.get("platform") == "telegram":
                    last = mtime
                continue
            if not rows or rows[0].get("platform") != "telegram":
                continue
            pending: dict[str, Any] | None = None
            for row in rows[1:]:
                ts = _parse_iso(row.get("timestamp"))
                if row.get("role") == "user":
                    pending = row
                elif row.get("role") == "assistant" and pending is not None:
                    uts = _parse_iso(pending.get("timestamp")) or ts
                    if uts and uts >= src.since:
                        items.append(_Item(uts, (
                            f"Suti: {_clip(pending.get('content') or '', 220)} -> "
                            f"Narada: {_clip(row.get('content') or '', 140)} (hermes)")))
                    if uts and (last is None or uts > last):
                        last = uts
                    pending = None
    return items, last


def _telegram(src: WakeSources) -> _Group:
    group = _Group("Telegram")
    last: datetime | None = None
    cycles = src.memory_root / "heartbeat" / "chat-cycles"
    if cycles.is_dir():
        names = sorted((p for p in cycles.glob("*-chat-*.md")), key=lambda p: p.name)
        for path in reversed(names):
            m = _BRIDGE_NAME_RE.match(path.name)
            stamp = (datetime.strptime(m.group(1), "%Y-%m-%d-%H%M%S").replace(
                tzinfo=timezone.utc) if m else None)
            if last is None and stamp is not None:
                last = stamp
            if stamp is not None and stamp < src.since - timedelta(minutes=5):
                break  # names sort by time; everything further is older
            item = _bridge_exchange(path)
            if item and item.ts >= src.since:
                group.items.append(item)
    hermes, hermes_last = _hermes_items(src)
    group.items.extend(hermes)
    if hermes_last and (last is None or hermes_last > last):
        last = hermes_last
    if not group.items:
        group.note = _none_since(last)
    return group


def _last_exchange(
    turns: list[tuple[str | None, str]], user: str, assistant: str,
) -> tuple[str, str | None]:
    """Suti's latest message and the reply that came *after* it.

    The reply is the *latest* Narada message after it ("Checking
    calendar" then "Cancelled" reports "Cancelled"), or None when that
    message is still unanswered. Picking the last of each independently
    would pin an earlier reply onto a newer, unanswered request. With no
    message from Suti at all, the latest Narada-initiated line is the reply.
    """
    idx = next((i for i in range(len(turns) - 1, -1, -1) if turns[i][0] == user), None)
    if idx is None:
        return "", next((t for w, t in reversed(turns) if w == assistant), None)
    reply = next((t for w, t in reversed(turns[idx + 1:]) if w == assistant), None)
    return turns[idx][1], reply


def _reply_text(reply: str | None) -> str:
    return "(no reply yet)" if reply is None else _clip(reply, 120)


def _app_chats(src: WakeSources) -> _Group:
    group = _Group("App chat")
    last: datetime | None = None
    root = src.memory_root / "brain" / "chats"
    for path in sorted(root.glob("*/*.json")) if root.is_dir() else []:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if mtime < src.since:
            last = max(last, mtime) if last else mtime
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        updated = doc.get("updatedAt")
        ts = (datetime.fromtimestamp(updated / 1000, timezone.utc)
              if isinstance(updated, (int, float)) else
              _parse_iso(doc.get("mirrored_at")) or mtime)
        last = max(last, ts) if last else ts
        if ts < src.since:
            continue
        msgs = [m for m in doc.get("messages") or [] if isinstance(m, dict)]
        said, reply = _last_exchange(
            [(m.get("role"), m.get("text") or "") for m in msgs], "user", "assistant")
        title = _clip(doc.get("title") or "untitled", 40)
        group.items.append(_Item(ts, (
            f"'{title}' ({len(msgs)} msgs): Suti: {_clip(said, 160)} -> "
            f"Narada: {_reply_text(reply)}")))
    if not group.items:
        group.note = _none_since(last)
    return group


def _voice(src: WakeSources) -> _Group:
    from smriti.daylog.adapters.voice_md import VoiceMdAdapter

    group = _Group("Voice")
    last: datetime | None = None
    root = src.memory_root / "heartbeat" / "voice-transcripts"
    adapter = VoiceMdAdapter()
    for path in sorted(root.glob("*/*.md")) if root.is_dir() else []:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if mtime < src.since:
            last = max(last, mtime) if last else mtime
            continue
        turns = [t for t in adapter.extract(path, path.read_bytes()).turns]
        if turns:
            last = max(last, turns[-1].ts) if last else turns[-1].ts
        turns = [t for t in turns if t.ts >= src.since]
        if not turns:
            continue
        said, reply = _last_exchange([(t.who, t.text) for t in turns], "suti", "narada")
        room = path.stem[16:] or path.stem
        group.items.append(_Item(turns[-1].ts, (
            f"{room} ({len(turns)} turns): Suti: {_clip(said, 160)} -> "
            f"Narada: {_reply_text(reply)}")))
    if not group.items:
        group.note = _none_since(last)
    return group


def _notifications(src: WakeSources) -> _Group:
    group = _Group("Sent to Suti")
    path = src.memory_root / "brain" / "notifications.json"
    if not path.is_file():
        group.note = f"no feed at {path}"
        return group
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else data
    last: datetime | None = None
    push: dict[str, int] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ts = _parse_iso(row.get("at"))
        if ts is None:
            continue
        last = max(last, ts) if last else ts
        if ts < src.since:
            continue
        delivered = row.get("delivered") or {}
        state = delivered.get("push") if isinstance(delivered, dict) else None
        if state:
            push[str(state)] = push.get(str(state), 0) + 1
        group.items.append(_Item(ts, (
            f"{row.get('source') or '?'}/{row.get('level') or '?'}: "
            f"{_clip(row.get('body') or '', 110)}")))
    if group.items and push:
        group.summary = "push " + ", ".join(f"{k} x{v}" for k, v in sorted(push.items()))
    if not group.items:
        group.note = _none_since(last)
    return group


_COLLECTORS: tuple[tuple[str, Callable[[WakeSources], _Group], float], ...] = (
    ("Telegram", _telegram, 0.45),
    ("App chat", _app_chats, 0.2),
    ("Voice", _voice, 0.2),
    ("Sent to Suti", _notifications, 0.15),
)


def _group_header(group: _Group, shown: int) -> str:
    if not group.items:
        return f"{group.title}: {group.note or 'none in 24h'}"
    n = len(group.items)
    head = f"{group.title}: {n} in 24h"
    if shown < n:
        head += f", newest {shown} shown"
    if group.summary:
        head += f" ({group.summary})"
    return head


def across_section(src: WakeSources) -> str:
    """Telegram, app chats, voice and notifications from the last 24h.

    Deterministic truncation: every source keeps its one-line header;
    item lines go newest first into per-source shares of what is left,
    then any leftover goes to the sources in priority order. Shown
    items read oldest to newest.
    """
    header = "--- ACROSS SURFACES, last 24h ---"
    groups: list[_Group] = []
    for title, collect, _share in _COLLECTORS:
        try:
            group = collect(src)
        except Exception as exc:  # noqa: BLE001 — fail open, per source
            group = _Group(title, note=f"unreadable ({_why(exc)})")
        group.items.sort(key=lambda it: it.ts, reverse=True)
        groups.append(group)
    lines = {id(g): [f"  {_stamp(it.ts)} {it.text}" for it in g.items] for g in groups}
    # Headers are mandatory; reserve room for the ", newest N shown" suffix.
    fixed = len(header) + 1 + sum(len(_group_header(g, len(g.items))) + 24 for g in groups)
    room = max(0, ACROSS_CAP - fixed)
    take = {id(g): 0 for g in groups}
    used = {id(g): 0 for g in groups}

    def fill(group: _Group, allowance: int) -> int:
        spent = 0
        rows = lines[id(group)]
        while take[id(group)] < len(rows):
            need = len(rows[take[id(group)]]) + 1
            if spent + need > allowance:
                break
            spent += need
            take[id(group)] += 1
        used[id(group)] += spent
        return spent

    left = room
    for group, (_t, _c, share) in zip(groups, _COLLECTORS):
        left -= fill(group, int(room * share))
    for group in groups:  # leftovers, in priority order
        left -= fill(group, left)
    out = [header]
    for group in groups:
        n = take[id(group)]
        out.append(_group_header(group, n))
        out.extend(reversed(lines[id(group)][:n]))
    return _fit_lines(out, ACROSS_CAP)
