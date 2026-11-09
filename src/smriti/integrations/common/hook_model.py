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

The interpreter is part of the semantics: the hook names the python that
ran the installer (``sys.executable``), as the MCP registration does
(:mod:`.mcp_spec`). A bare ``python`` resolves against the PATH of
whatever launched the harness, which can lead to an interpreter without
smriti, and the briefing is then missing. A hook that runs another
interpreter (the bare ``python`` of earlier installs included) is
DEFICIENT, so a reinstall brings it up to date and doctor names the gap.
"""

from __future__ import annotations

import base64
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

_VALID_FRAMINGS = ("raw", "codex-json")

EQUIVALENT = "equivalent"
DEFICIENT = "deficient"
UNRELATED = "unrelated"


# --- generation -----------------------------------------------------------

def installing_interpreter(interpreter: str | None = None) -> str:
    """The python a wake hook runs: ``interpreter``, else the one running now.

    ``sys.executable`` can be empty (an embedded interpreter); the hook
    then falls back to ``python`` from PATH, as it ran before.
    """
    return interpreter or sys.executable or "python"


def _sh_interpreter(python: str) -> str:
    """``python`` as one POSIX-shell word: single-quoted, so spaces and ``$``
    stay literal (an embedded quote is written ``'"'"'``). A Windows path is
    written with forward slashes, which Git Bash reads as well, so the sh
    form holds no backslash."""
    if PureWindowsPath(python).drive:
        python = python.replace("\\", "/")
    return "'" + python.replace("'", "'\"'\"'") + "'"


def _ps_interpreter(python: str) -> str:
    """``python`` run by PowerShell's call operator, as a single-quoted
    string (an embedded quote doubled)."""
    return "& '" + python.replace("'", "''") + "'"


def make_wake_hook_command(
    memory_root: Path,
    *,
    home: Path,
    framing: str = "raw",
    audience: str = "coding",
    style: str = "sh",
    interpreter: str | None = None,
) -> str:
    """Command string a harness runs at SessionStart to fire wake.py.

    ``style="sh"`` emits the POSIX form (Claude Code runs hook commands
    under a POSIX shell on every platform, including Windows).
    ``style="powershell-encoded"`` emits
    ``powershell.exe -NoProfile -NonInteractive -EncodedCommand <b64>``
    — required for Codex on Windows, which runs hooks under PowerShell,
    NOT bash; a sh-form command string does not survive there.

    Either form runs wake.py with ``interpreter`` (default: the python
    running the installer, :func:`installing_interpreter`) by its path,
    not with a ``python`` looked up on the harness's PATH.
    """
    if framing not in _VALID_FRAMINGS:
        raise ValueError(f"unknown wake framing {framing!r}")
    memory_rel = memory_root.relative_to(home).as_posix()
    python = installing_interpreter(interpreter)

    if style == "sh":
        framing_var = (
            f'SMRITI_WAKE_FRAMING="{framing}" ' if framing != "raw" else ""
        )
        return (
            f'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/{memory_rel}" '
            f'SMRITI_WAKE_AUDIENCE="{audience}" {framing_var}'
            f'{_sh_interpreter(python)} "$HOME/{memory_rel}/.smriti/wake.py"'
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
            f"{_ps_interpreter(python)} (Join-Path $HOME '{memory_rel}/.smriti/wake.py')"
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
    interpreter: str = ""  # what runs it: a path, or a bare name looked up on PATH


ENCODED_RE = re.compile(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", re.IGNORECASE)
_PS_ENV_LITERAL_RE = re.compile(r"\$env:(\w+)\s*=\s*'([^']*)'")
_PS_ENV_JOINPATH_RE = re.compile(
    r"\$env:(\w+)\s*=\s*\(Join-Path \$HOME '([^']*)'\)"
)
# PowerShell's interpreter: the call operator and a single-quoted path (the
# generated form), or a bare python (earlier installs).
_PS_WAKE_RE = re.compile(
    r"(?:&\s*'(?P<path>(?:[^']|'')*)'|(?P<bare>python3?(?:\.exe)?))"
    r"\s+\(Join-Path \$HOME '(?P<wake>[^']*wake\.py)'\)"
)
_SH_ENV_RE = re.compile(r'\b(SMRITI_\w+)=("([^"]*)"|\'([^\']*)\'|(\S+))')
# The POSIX shell's interpreter, one word: single-quoted (the generated form;
# an embedded quote is '"'"'), double-quoted, or unquoted and naming a python
# (the bare python or python3 of earlier installs, or a path to one). Doctor
# finds the scripts hooks run with the same pattern.
SH_INTERPRETER = r"""(?:'[^']*'(?:"'"'[^']*')*|"[^"]*"|[^\s'";&|()<>]*python[\w.]*)"""
_SH_WAKE_RE = re.compile(
    rf'(?P<interp>{SH_INTERPRETER})\s+"?\$HOME/(?P<wake>[^"\s]*wake\.py)"?'
)


def _normalize(rel: str) -> str:
    return rel.replace("\\", "/").strip("/")


def _sh_unquote(word: str) -> str:
    """The interpreter a POSIX-shell word names, without its quotes."""
    if word[:1] in ("'", '"'):
        try:
            return "".join(shlex.split(word))
        except ValueError:
            return word.strip("'\"")
    return word


def _interpreter_key(python: str) -> str:
    """``python`` for comparison: a Windows path in either slash and any case."""
    if PureWindowsPath(python).drive:
        return python.replace("\\", "/").lower()
    return python


def parse_wake_command(command: str) -> WakeHookSpec | None:
    """Extract wake semantics from an sh or PowerShell-encoded command.

    Returns None when the command does not invoke a ``wake.py`` at all
    (i.e. it is not a wake hook of any lineage — UNRELATED territory).
    """
    encoded = ENCODED_RE.search(command)
    if encoded:
        try:
            script = base64.b64decode(encoded.group(1)).decode("utf-16-le")
        except (ValueError, UnicodeDecodeError):
            return None
        wake = _PS_WAKE_RE.search(script)
        if not wake:
            return None
        path = wake.group("path")
        interpreter = path.replace("''", "'") if path is not None else wake.group("bare")
        env: dict[str, str] = {}
        for name, value in _PS_ENV_LITERAL_RE.findall(script):
            if name.startswith("SMRITI_"):
                env[name] = value
        for name, rel in _PS_ENV_JOINPATH_RE.findall(script):
            if name.startswith("SMRITI_"):
                env[name] = f"$HOME/{_normalize(rel)}"
        return WakeHookSpec(wake_path=_normalize(wake.group("wake")), env=env,
                            interpreter=interpreter)

    wake = _SH_WAKE_RE.search(command)
    if not wake:
        return None
    env = {}
    for m in _SH_ENV_RE.finditer(command):
        env[m.group(1)] = m.group(3) or m.group(4) or m.group(5) or ""
    return WakeHookSpec(wake_path=_normalize(wake.group("wake")), env=env,
                        interpreter=_sh_unquote(wake.group("interp")))


# --- classification -------------------------------------------------------

_WAKE_ON = {"1", "full", "on", "true", "yes"}


def classify_wake_hook(
    command: str,
    *,
    memory_root: Path,
    home: Path,
    framing: str = "raw",
    audience: str = "coding",
    interpreter: str | None = None,
) -> tuple[str, str]:
    """Classify a hook command against the canonical wake semantics.

    Returns ``(verdict, detail)`` where verdict is EQUIVALENT,
    DEFICIENT, or UNRELATED. DEFICIENT means "this is our wake hook but
    its semantics are wrong" — the installer should upgrade it and the
    doctor should FAIL with ``detail`` naming the gaps. The hook must run
    ``interpreter`` (default: :func:`installing_interpreter`, the python
    classifying it); a bare ``python`` from PATH, or another python, is a gap.
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
    expected_python = installing_interpreter(interpreter)
    if _interpreter_key(spec.interpreter) != _interpreter_key(expected_python):
        found = spec.interpreter
        if "/" not in found and "\\" not in found:
            found += " from PATH"
        gaps.append(f"runs {found}, not {expected_python}")

    if gaps:
        return DEFICIENT, "; ".join(gaps)
    return EQUIVALENT, "wake semantics match"


__all__ = [
    "EQUIVALENT",
    "DEFICIENT",
    "UNRELATED",
    "SH_INTERPRETER",
    "WakeHookSpec",
    "installing_interpreter",
    "make_wake_hook_command",
    "parse_wake_command",
    "classify_wake_hook",
]
