"""PreCompact hook — capture conversation turns as events before compaction.

User-level Claude Code hook. Runs on EVERY project's PreCompact event
(wired via ~/.claude/settings.json). Captures the visible conversation
turns that would otherwise be lost when Claude Code compacts context.

Entity namespace is derived from the working directory:
    C:\\Projects\\svapna            → events entity = "svapna-narada"
    C:\\Projects\\beautiful-tree    → events entity = "beautiful-tree-narada"
    C:\\Projects\\narada-memory     → events entity = "narada-memory-narada"
    ...and so on

This matches smriti's `events/{entity}/` namespace layout. When smriti
v0.1 lands, the staging tree at ~/.claude/narada-staging-events/
migrates to ~/.narada/memory/events/ with one `mv`, and the cascade
picks up the accumulated turns as a bulk ingest.

Design principles:
    - Defensive. A PreCompact hook failure must NOT block the user's
      session. On any error, log to stderr and exit 0.
    - Idempotent + incremental. A per-session marker tracks the last
      JSONL offset processed, so repeated runs only capture new turns.
    - Project-agnostic. Entity name derived from cwd basename, not
      hardcoded. Works for any Claude Code project.
    - Minimal. Captures role/content/timestamp per turn. No thinking
      blocks (those are private + volatile). No tool-use internals
      beyond a readable summary (tool name + input as fenced JSON).

Stdin payload (from Claude Code):
    {
        "session_id": "<uuid>",
        "cwd": "C:\\Projects\\<project>",
        "hook_event_name": "PreCompact",
        "trigger": "manual" | "auto"
    }

Transcript location (derived, because payload `transcript_path` is
unreliable per anthropics/claude-code#13668):
    project_hash = re.sub(r'[:\\/]', '-', cwd)
    transcript = ~/.claude/projects/{project_hash}/{session_id}.jsonl

Output layout:
    ~/.claude/narada-staging-events/
    ├── .markers/
    │   └── {session_id}.json
    ├── svapna-narada/
    │   └── YYYY/MM/YYYY-MM-DD/{sess8}-turn-{NNNN}.md
    ├── beautiful-tree-narada/
    │   └── YYYY/MM/YYYY-MM-DD/{sess8}-turn-{NNNN}.md
    └── ...
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


STAGING_ROOT = Path.home() / ".claude" / "narada-staging-events"


def log(msg: str) -> None:
    """Print to stderr — stdout is ignored by PreCompact hooks, stderr
    shows up in Claude Code's hook log."""
    print(f"[precompact_capture] {msg}", file=sys.stderr)


def project_hash_from_cwd(cwd: str) -> str:
    """Convert a working-directory path to the Claude Code project-hash
    folder name used under ~/.claude/projects/.

    Observed rule:
        "C:\\Projects\\svapna"      -> "C--Projects-svapna"
        "/home/user/proj"           -> "-home-user-proj"
    Each `:`, `\\`, or `/` becomes a `-` (not collapsed).
    """
    return re.sub(r"[:\\/]", "-", cwd)


def entity_from_cwd(cwd: str) -> str:
    """Derive the events-namespace entity from the cwd.

    `C:\\Projects\\svapna` → `svapna-narada`
    `/home/user/beautiful-tree` → `beautiful-tree-narada`

    Empty/missing cwd falls back to `unknown-narada`. This is the thing
    that keeps smriti's `events/{entity}/` layout working across projects
    without per-project hook configuration.
    """
    if not cwd:
        return "unknown-narada"
    name = Path(cwd).name or "unknown"
    # Sanitize for filesystem safety: lowercase, keep alnum + dash
    name = re.sub(r"[^a-zA-Z0-9._-]", "-", name).strip("-").lower()
    return f"{name}-narada"


def transcript_path_for(session_id: str, cwd: str) -> Path:
    """Locate the JSONL transcript for a given session."""
    home = Path.home()
    project_hash = project_hash_from_cwd(cwd)
    return home / ".claude" / "projects" / project_hash / f"{session_id}.jsonl"


def marker_path_for(session_id: str) -> Path:
    return STAGING_ROOT / ".markers" / f"{session_id}.json"


def load_marker(session_id: str) -> dict:
    """Read the per-session marker, or return a fresh one."""
    path = marker_path_for(session_id)
    if not path.exists():
        return {"session_id": session_id, "last_line": 0, "turn_count": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"marker corrupt, resetting: {e}")
        return {"session_id": session_id, "last_line": 0, "turn_count": 0}


def save_marker(marker: dict) -> None:
    path = marker_path_for(marker["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker, indent=2), encoding="utf-8")


def extract_turn(entry: dict) -> tuple[str, str, str] | None:
    """Return (role, timestamp, content) for a user/assistant turn,
    or None if this JSONL entry isn't a turn we want to capture.

    Skips: file-history-snapshot, permission-mode, system/bridge_status,
    hook_success attachments, thinking blocks. Captures: text + tool_use
    + tool_result blocks.
    """
    if entry.get("type") not in ("user", "assistant"):
        return None

    message = entry.get("message")
    if not message or not isinstance(message, dict):
        return None

    role = message.get("role")
    if role not in ("user", "assistant"):
        return None

    ts = entry.get("timestamp") or message.get("timestamp") or ""

    content = message.get("content")
    if isinstance(content, str):
        return role, ts, content

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_name = block.get("name", "unknown")
                tool_input = block.get("input", {})
                try:
                    tool_input_str = json.dumps(tool_input, indent=2)[:2000]
                except Exception:
                    tool_input_str = str(tool_input)[:2000]
                parts.append(
                    f"\n[tool_use: {tool_name}]\n```json\n{tool_input_str}\n```"
                )
            elif btype == "tool_result":
                result = block.get("content", "")
                if isinstance(result, list):
                    result = json.dumps(result)[:2000]
                parts.append(f"\n[tool_result]\n{str(result)[:2000]}")
            # thinking blocks explicitly skipped
        joined = "\n".join(p for p in parts if p).strip()
        if not joined:
            return None
        return role, ts, joined

    return None


def event_file_path(
    turn_ts: str, session_id: str, turn_n: int, entity: str
) -> Path:
    """Build the event file path. Groups by capture date (UTC) when the
    turn doesn't carry a timestamp."""
    if turn_ts:
        try:
            dt = datetime.fromisoformat(turn_ts.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)

    date_dir = dt.strftime("%Y/%m/%Y-%m-%d")
    sess8 = session_id[:8]
    filename = f"{sess8}-turn-{turn_n:04d}.md"
    return STAGING_ROOT / entity / date_dir / filename


def write_event(
    path: Path,
    session_id: str,
    turn_n: int,
    role: str,
    ts: str,
    content: str,
    trigger: str,
    entity: str,
    cwd: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now(timezone.utc).isoformat()
    frontmatter = (
        "---\n"
        f"session_id: {session_id}\n"
        f"turn_number: {turn_n}\n"
        f"role: {role}\n"
        f"timestamp: {ts or captured_at}\n"
        f"captured_at: {captured_at}\n"
        f"captured_by: precompact-hook\n"
        f"trigger: {trigger}\n"
        f"entity: {entity}\n"
        f"cwd: {cwd}\n"
        "---\n\n"
    )
    path.write_text(frontmatter + content + "\n", encoding="utf-8")


def process_transcript(
    transcript: Path, marker: dict, trigger: str, entity: str, cwd: str
) -> int:
    """Read the transcript from the marker position, write any new turns
    as event files. Return the number of turns written."""
    if not transcript.exists():
        log(f"transcript not found at {transcript} — skipping")
        return 0

    last_line = marker.get("last_line", 0)
    turn_count = marker.get("turn_count", 0)
    session_id = marker["session_id"]
    written = 0
    final_lineno = last_line - 1  # in case the file is empty after last_line

    with transcript.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f):
            final_lineno = lineno
            if lineno < last_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            turn = extract_turn(entry)
            if turn is None:
                continue

            role, ts, content = turn
            turn_count += 1
            event_path = event_file_path(ts, session_id, turn_count, entity)

            try:
                write_event(
                    event_path,
                    session_id,
                    turn_count,
                    role,
                    ts,
                    content,
                    trigger,
                    entity,
                    cwd,
                )
                written += 1
            except Exception as e:
                log(f"write failed for turn {turn_count}: {e}")
                turn_count -= 1  # rollback so retry next time

    # Always advance the marker so we don't re-scan already-processed lines,
    # even if no new turns were extractable in that range.
    marker["last_line"] = final_lineno + 1
    marker["turn_count"] = turn_count
    marker["last_run"] = datetime.now(timezone.utc).isoformat()
    marker["entity"] = entity
    return written


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw:
            log("no stdin payload; nothing to do")
            return 0

        payload = json.loads(raw)
        session_id = payload.get("session_id")
        cwd = payload.get("cwd", "")
        trigger = payload.get("trigger", "unknown")

        if not session_id:
            log("payload missing session_id; cannot locate transcript")
            return 0

        entity = entity_from_cwd(cwd)
        transcript = transcript_path_for(session_id, cwd)
        marker = load_marker(session_id)
        written = process_transcript(transcript, marker, trigger, entity, cwd)

        save_marker(marker)

        if written > 0:
            log(
                f"captured {written} new turns to staging "
                f"(entity={entity}, session={session_id[:8]}, "
                f"trigger={trigger}, total={marker['turn_count']})"
            )
        else:
            log(
                f"no new turns to capture "
                f"(entity={entity}, session={session_id[:8]}, trigger={trigger})"
            )
        return 0

    except Exception as e:
        log(f"unexpected error: {e}")
        log(traceback.format_exc())
        return 0  # never block compaction


if __name__ == "__main__":
    sys.exit(main())
