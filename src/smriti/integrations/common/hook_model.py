"""Canonical wake-hook model — one classifier for installers AND doctor.

A SessionStart wake hook is "working" only if it (a) targets the
entity's ``wake.py`` and (b) carries the right environment: SMRITI_WAKE
on, SMRITI_ROOT at the entity tree, the harness-correct framing, and an
audience. "Targets wake.py" alone is too weak (a hook can hit the right
file with the wrong env and silently wake without identity), and exact
string comparison is too strong (Codex on Windows runs hooks under
PowerShell, so the live command is a ``-EncodedCommand`` blob that is
semantically identical to the sh form).

This module is the single source of truth both sides share:

    installers  — leave EQUIVALENT hooks untouched, upgrade DEFICIENT
                  ones (right wake.py, wrong semantics) with a config
                  backup, never modify UNRELATED hooks.
    doctor      — PASS on EQUIVALENT, FAIL with the specific gap on
                  DEFICIENT, ignore UNRELATED.

Command generation lives here too (moved from mcp_spec) so the
generator and the classifier can never drift apart.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path

_VALID_FRAMINGS = ("raw", "codex-json")

EQUIVALENT = "equivalent"
DEFICIENT = "deficient"
UNRELATED = "unrelated"


# --- generation -----------------------------------------------------------

def make_wake_hook_command(
    memory_root: Path,
    *,
    home: Path,
    framing: str = "raw",
    audience: str = "coding",
    style: str = "sh",
) -> str:
    """Command string a harness runs at SessionStart to fire wake.py.

    ``style="sh"`` emits the POSIX form (Claude Code runs hook commands
    under a POSIX shell on every platform, including Windows).
    ``style="powershell-encoded"`` emits
    ``powershell.exe -NoProfile -NonInteractive -EncodedCommand <b64>``
    — required for Codex on Windows, which runs hooks under PowerShell,
    NOT bash; a sh-form command string does not survive there.
    """
    if framing not in _VALID_FRAMINGS:
        raise ValueError(f"unknown wake framing {framing!r}")
    memory_rel = memory_root.relative_to(home).as_posix()

    if style == "sh":
        framing_var = (
            f'SMRITI_WAKE_FRAMING="{framing}" ' if framing != "raw" else ""
        )
        return (
            f'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/{memory_rel}" '
            f'SMRITI_WAKE_AUDIENCE="{audience}" {framing_var}'
            f'python "$HOME/{memory_rel}/.smriti/wake.py"'
        ).strip()

    if style == "powershell-encoded":
        parts = [
            "$ProgressPreference='SilentlyContinue'",
            "$ErrorActionPreference='Stop'",
            "$env:SMRITI_WAKE='1'",
            f"$env:SMRITI_ROOT=(Join-Path $HOME '{memory_rel}')",
            f"$env:SMRITI_WAKE_AUDIENCE='{audience}'",
        ]
        if framing != "raw":
            parts.append(f"$env:SMRITI_WAKE_FRAMING='{framing}'")
        parts.append(
            f"python (Join-Path $HOME '{memory_rel}/.smriti/wake.py')"
        )
        script = "; ".join(parts)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return (
            "powershell.exe -NoProfile -NonInteractive "
            f"-EncodedCommand {encoded}"
        )

    raise ValueError(f"unknown command style {style!r}")


# --- parsing --------------------------------------------------------------

@dataclass(frozen=True)
class WakeHookSpec:
    """Semantics extracted from a wake-hook command string."""

    wake_path: str  # normalized, $HOME-relative posix path to wake.py
    env: dict[str, str] = field(default_factory=dict)


_ENCODED_RE = re.compile(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", re.IGNORECASE)
_PS_ENV_LITERAL_RE = re.compile(r"\$env:(\w+)\s*=\s*'([^']*)'")
_PS_ENV_JOINPATH_RE = re.compile(
    r"\$env:(\w+)\s*=\s*\(Join-Path \$HOME '([^']*)'\)"
)
_PS_WAKE_RE = re.compile(r"python\s+\(Join-Path \$HOME '([^']*wake\.py)'\)")
_SH_ENV_RE = re.compile(r'\b(SMRITI_\w+)=("([^"]*)"|\'([^\']*)\'|(\S+))')
_SH_WAKE_RE = re.compile(r'python3?\s+"?\$HOME/([^"\s]*wake\.py)"?')


def _normalize(rel: str) -> str:
    return rel.replace("\\", "/").strip("/")


def parse_wake_command(command: str) -> WakeHookSpec | None:
    """Extract wake semantics from an sh or PowerShell-encoded command.

    Returns None when the command does not invoke a ``wake.py`` at all
    (i.e. it is not a wake hook of any lineage — UNRELATED territory).
    """
    encoded = _ENCODED_RE.search(command)
    if encoded:
        try:
            script = base64.b64decode(encoded.group(1)).decode("utf-16-le")
        except (ValueError, UnicodeDecodeError):
            return None
        wake = _PS_WAKE_RE.search(script)
        if not wake:
            return None
        env: dict[str, str] = {}
        for name, value in _PS_ENV_LITERAL_RE.findall(script):
            if name.startswith("SMRITI_"):
                env[name] = value
        for name, rel in _PS_ENV_JOINPATH_RE.findall(script):
            if name.startswith("SMRITI_"):
                env[name] = f"$HOME/{_normalize(rel)}"
        return WakeHookSpec(wake_path=_normalize(wake.group(1)), env=env)

    wake = _SH_WAKE_RE.search(command)
    if not wake:
        return None
    env = {}
    for m in _SH_ENV_RE.finditer(command):
        env[m.group(1)] = m.group(3) or m.group(4) or m.group(5) or ""
    return WakeHookSpec(wake_path=_normalize(wake.group(1)), env=env)


# --- classification -------------------------------------------------------

_WAKE_ON = {"1", "full", "on", "true", "yes"}


def classify_wake_hook(
    command: str,
    *,
    memory_root: Path,
    home: Path,
    framing: str = "raw",
    audience: str = "coding",
) -> tuple[str, str]:
    """Classify a hook command against the canonical wake semantics.

    Returns ``(verdict, detail)`` where verdict is EQUIVALENT,
    DEFICIENT, or UNRELATED. DEFICIENT means "this is our wake hook but
    its semantics are wrong" — the installer should upgrade it and the
    doctor should FAIL with ``detail`` naming the first gap.
    """
    spec = parse_wake_command(command)
    if spec is None:
        return UNRELATED, "does not invoke a wake.py"

    memory_rel = memory_root.relative_to(home).as_posix()
    expected_wake = _normalize(f"{memory_rel}/.smriti/wake.py")
    if spec.wake_path != expected_wake:
        return UNRELATED, f"targets {spec.wake_path}, not {expected_wake}"

    gaps: list[str] = []
    if spec.env.get("SMRITI_WAKE", "").strip().lower() not in _WAKE_ON:
        gaps.append("SMRITI_WAKE not enabled")
    expected_root = f"$HOME/{memory_rel}"
    if _normalize(spec.env.get("SMRITI_ROOT", "").replace("$HOME/", "")) != _normalize(memory_rel):
        gaps.append(f"SMRITI_ROOT != {expected_root}")
    actual_framing = spec.env.get("SMRITI_WAKE_FRAMING", "raw") or "raw"
    if actual_framing != framing:
        gaps.append(f"framing {actual_framing!r} != {framing!r}")
    actual_audience = spec.env.get("SMRITI_WAKE_AUDIENCE", "coding") or "coding"
    if actual_audience != audience:
        gaps.append(f"audience {actual_audience!r} != {audience!r}")

    if gaps:
        return DEFICIENT, "; ".join(gaps)
    return EQUIVALENT, "wake semantics match"


__all__ = [
    "EQUIVALENT",
    "DEFICIENT",
    "UNRELATED",
    "WakeHookSpec",
    "make_wake_hook_command",
    "parse_wake_command",
    "classify_wake_hook",
]
