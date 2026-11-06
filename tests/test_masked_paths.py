"""H12.1: a credential shape planted in each path into the tree is masked in the file.

One test per path: the MCP project tools (a todo, a finding with its
per-tag stubs and the cross-project index, a decision, a feature spec and
its status notes), the handoff, an ``index.md`` listing, a rendered day,
and what the nightly's model writes (a digest, a rollup), where a fake
provider answers with credential shapes, as a model made to copy them by
an injection would. The reference profile's outbox has its own tests, with
the profile. The credentials are synthetic, split so no scanner takes them
for real ones.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from smriti.core.tree import tree_root
from smriti.daylog.config import DaylogConfig
from smriti.llm.types import LLMRequest, LLMResponse

KEY = "sk-ant-api03-" + "abcdefghijklmnop1234"
TOKEN = "ghp_" + "ABCDEFGHIJKLMNOPQRST1234"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
CANARIES = (KEY, TOKEN, AWS)


def _masked(text: str) -> bool:
    return "[redacted]" in text and not any(c in text for c in CANARIES)


def _reply(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from smriti.mcp.server import handle_message

    reply = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
    assert reply is not None
    result: dict[str, Any] = reply["result"]
    return result


def _call(name: str, arguments: dict[str, Any]) -> str:
    result = _reply(name, arguments)
    assert not result.get("isError"), result
    text: str = result["content"][0]["text"]
    return text


def _tree_files() -> dict[str, str]:
    """The tree's files, by path relative to the root, but for the metrics log."""
    root = tree_root()
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in root.rglob("*") if p.is_file() and p.suffix != ".jsonl"}


def _mirror() -> Path:
    mirror = tree_root() / "mirrors" / "demo"
    (mirror / "ai").mkdir(parents=True)
    return mirror


# ------------------------------------------------------------ the MCP tools


def test_a_todo_is_masked() -> None:
    todo = _mirror() / "ai" / "todo.md"
    todo.write_text("# TODO\n\n## Active\n", encoding="utf-8")
    _call("smriti_add_todo", {"project": "demo", "topic": f"rotate {KEY}",
                              "line": f"- [ ] rotate {KEY} and {TOKEN}", "kind": "fix",
                              "source": f"[[notes/{AWS}]]"})
    text = todo.read_text(encoding="utf-8")
    assert _masked(text) and "- [ ] rotate [redacted]" in text
    # The topic is looked for masked, so the same todo isn't added twice.
    again = _call("smriti_add_todo", {"project": "demo", "topic": f"rotate {KEY}",
                                      "line": f"- [ ] rotate {KEY}", "kind": "fix"})
    assert again.startswith("Skipped")


def test_a_finding_its_stubs_and_the_cross_project_index_are_masked() -> None:
    _mirror()
    _call("smriti_record_finding", {
        "project": "demo", "title": f"Leaked {KEY}", "finding": f"Sam pasted {TOKEN}.",
        "why_it_matters": f"Ari saw {AWS}.", "tags": [f"keys {KEY}", "locks"],
        "source": f"session {TOKEN}"})
    files = _tree_files()
    written = [p for p in files if "findings" in p]
    assert len(written) == 4, written  # the finding, two stubs, the index
    for path in written:
        assert not any(c in path for c in CANARIES), path  # the names, too
        assert not any(c in files[path] for c in CANARIES), path
    [finding] = [p for p in written if p.startswith("mirrors/demo/findings/")]
    assert "# Leaked [redacted]" in files[finding] and "Sam pasted [redacted]." in files[finding]


def test_a_decision_is_masked() -> None:
    _mirror()
    _call("smriti_record_decision", {
        "project": "demo", "title": f"Rotate {AWS}", "decision": f"Revoke {KEY}.",
        "rationale": f"{TOKEN} leaked.", "alternatives_considered": f"Keep {KEY}.",
        "source": f"thread {TOKEN}"})
    [(path, text)] = [(p, t) for p, t in _tree_files().items() if "/decisions/" in p]
    assert not any(c in path for c in CANARIES) and _masked(text)
    assert "## Decision\n\nRevoke [redacted]." in text


def test_a_feature_spec_and_its_status_notes_are_masked() -> None:
    mirror = _mirror()
    _call("smriti_record_feature", {
        "project": "demo", "slug": "rotate-keys", "title": f"Rotate {KEY}",
        "goal": f"No {TOKEN} in the tree.", "plan": f"Revoke {AWS}.",
        "why_now": f"{KEY} leaked", "constraints": f"keep {TOKEN} out"})
    spec = mirror / "ai" / "features" / "active" / "rotate-keys.md"
    assert _masked(spec.read_text(encoding="utf-8"))
    _call("smriti_update_feature_status", {"project": "demo", "slug": "rotate-keys",
                                           "new_status": "shipped",
                                           "notes": f"Revoked {KEY} and {AWS}."})
    text = (mirror / "ai" / "features" / "shipped" / "rotate-keys.md").read_text(encoding="utf-8")
    assert _masked(text) and "Revoked [redacted] and [redacted]." in text


def test_a_feature_slug_shaped_like_a_credential_is_refused() -> None:
    """The slug names the file for the feature's life: refused, not renamed."""
    _mirror()
    result = _reply("smriti_record_feature", {"project": "demo", "slug": KEY, "title": "t",
                                              "goal": "g", "plan": "p"})
    assert result.get("isError") is True and "credential" in result["content"][0]["text"]
    assert not any(KEY in p for p in _tree_files())


def test_the_handoff_is_masked() -> None:
    _call("smriti_write_handoff", {"content": f"Next: revoke {KEY}; {TOKEN} is in .env.",
                                   "summary": f"keys {AWS}"})
    text = (tree_root() / ".smriti" / "handoff-pending.md").read_text(encoding="utf-8")
    assert _masked(text) and "Next: revoke [redacted]" in text


def test_an_index_page_listing_a_heading_with_a_credential_is_masked() -> None:
    from smriti.store.index_pages import update_index_pages

    notes = tree_root() / "notes"
    notes.mkdir()
    (notes / "index.md").write_text("# Notes\n", encoding="utf-8")
    by_hand = notes / "by-hand.md"
    by_hand.write_text(f"# Keys {KEY}\n\nwritten by hand\n", encoding="utf-8")
    assert update_index_pages(by_hand, tree_root())
    listing = (notes / "index.md").read_text(encoding="utf-8")
    assert _masked(listing) and "— Keys [redacted]" in listing
    # Unchanged once masked, so the next write in the directory leaves it.
    assert update_index_pages(by_hand, tree_root()) == []


# ------------------------------------------------- what the nightly writes


class _Leaky:
    """An LLM whose answer carries credential shapes, as an injected model's might."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def is_available(self) -> bool:
        return True

    def call(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=f"## Highlights\n- Sam's key {KEY}\n- token: {TOKEN}\n")


@pytest.fixture
def leaky(monkeypatch: pytest.MonkeyPatch) -> _Leaky:
    from smriti.daylog import digest

    provider = _Leaky()
    monkeypatch.setattr(digest, "get_provider", lambda name=None: provider)
    return provider


def _day(day: str, text: str) -> DaylogConfig:
    """A day of one turn written straight to its JSONL, as before capture masked turns."""
    cfg = DaylogConfig(root=tree_root(), user_name="Sam", agent_name="Ari")
    path = cfg.day_jsonl(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": f"voice:{day}", "ts": f"{day}T01:00:00Z",
                                "channel": "voice", "who": "user", "text": text}) + "\n",
                    encoding="utf-8")
    return cfg


def test_a_rendered_day_is_masked_where_capture_was_not() -> None:
    from smriti.daylog.render import render_day

    cfg = _day("2026-09-04", f"Sam read out {KEY} and {AWS}")
    out = render_day(cfg, "2026-09-04")
    assert out is not None and _masked(out.read_text(encoding="utf-8"))


def test_a_digest_is_masked_when_the_model_copies_a_credential(leaky: _Leaky) -> None:
    from smriti.daylog.digest import digest_day
    from smriti.daylog.render import render_day

    cfg = _day("2026-09-04", "an ordinary morning")
    render_day(cfg, "2026-09-04")
    out = digest_day(cfg, "2026-09-04", [])
    assert out is not None and len(leaky.requests) == 1
    text = out.read_text(encoding="utf-8")
    assert _masked(text) and "- Sam's key [redacted]" in text


def test_a_rollup_is_masked_when_the_model_copies_a_credential(leaky: _Leaky) -> None:
    from smriti.daylog.rollup import build_rollup, enumerate_stale_periods

    cfg = _day("2026-09-01", "unused")
    cfg.day_digest("2026-09-01").write_text(
        "---\ninput_hash: x\n---\n\n## Highlights\n- a quiet day\n", encoding="utf-8")
    [week] = [p for p in enumerate_stale_periods(cfg, date(2026, 9, 20)) if p.kind == "week"]
    out = build_rollup(week, [], cfg)
    assert len(leaky.requests) == 1 and _masked(out.read_text(encoding="utf-8"))


# ------------------------------------------------------- the append itself


def test_an_append_masks_and_takes_a_crlf_files_ending() -> None:
    from smriti.core.memwrite import append_memory

    path = tree_root() / "outbox" / "comms.md"
    path.parent.mkdir()
    path.write_bytes(b"# Comms\r\n\r\n")
    append_memory(path, f"## Ask\n\nrotate {KEY}\n", header="# unused\n")
    assert path.read_bytes() == b"# Comms\r\n\r\n## Ask\r\n\r\nrotate [redacted]\r\n"
