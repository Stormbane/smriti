"""Install and synchronize Hermes's generated ``SOUL.md``.

Hermes reads ``~/.hermes/SOUL.md`` directly; it does not execute Smriti's
wake runner.  Its identity therefore has to be materialized from the shared
core wake context plus the Hermes-only audience overlay.
"""

from __future__ import annotations

from pathlib import Path


HOME = Path.home()
HERMES = HOME / ".hermes"
SOUL_MD = HERMES / "SOUL.md"


def compose_soul(memory_root: Path, soul_path: Path | None = None) -> Path:
    """Atomically write core + Hermes context to the live ``SOUL.md``.

    Atomic replacement is intentional: it replaces the legacy hardlink
    without ever writing through it into ``wake-context.md``.
    """
    core = memory_root / ".smriti" / "wake-context.md"
    overlay = memory_root / ".smriti" / "context" / "hermes.md"
    destination = soul_path or SOUL_MD

    core_text = core.read_text(encoding="utf-8").rstrip()
    overlay_text = overlay.read_text(encoding="utf-8").strip()
    composed = f"{core_text}\n\n--- HERMES CONTEXT ---\n{overlay_text}\n"

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.tmp")
    temporary.write_text(composed, encoding="utf-8")
    temporary.replace(destination)
    return destination


def run(memory_root: Path) -> None:
    destination = compose_soul(memory_root)
    print(f"[hermes] composed {destination} from core + Hermes context")
