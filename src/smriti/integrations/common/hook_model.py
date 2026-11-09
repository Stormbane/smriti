"""Canonical wake-hook model — one classifier for installers AND doctor.

A SessionStart wake hook runs the installed package's wake,
``<python> -P -m smriti.wake`` (:data:`WAKE_ARGS`). It is "working" only if
it also carries the right environment: SMRITI_WAKE on, SMRITI_ROOT at the
entity tree, the harness-correct framing, and an audience. "Runs the wake"
alone is too weak (a hook can run it with the wrong env and silently wake
without identity), and exact string comparison is too strong (Codex on
Windows runs hooks under PowerShell, so the live command is a
``-EncodedCommand`` blob that is semantically identical to the sh form).

Earlier installs' hooks ran ``<root>/.smriti/wake.py``, a script the
installer copied into the memory tree, which agents write to and a restore
brings back from git: a write or a restore could change what every session
start ran. Such a hook, for this tree, is ours and DEFICIENT, so a
reinstall turns it into the module form in place and doctor names the gap.
One for another tree's ``wake.py`` is UNRELATED, as is a module-form hook
that wakes another tree. Which tree a module-form hook wakes is the one
smriti's own rule chooses (:func:`smriti.core.tree.choose_root`) for the
hook's SMRITI_ROOT or NARADA_ROOT, and, where it sets neither, for the
environment as the installer sees it: a hook that names no root is not
claimed by whichever root is being installed.

This module is the single source of truth both sides share:

    installers  — leave EQUIVALENT hooks untouched, upgrade DEFICIENT
                  ones (ours, wrong semantics) with a config backup,
                  never modify UNRELATED hooks.
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
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from smriti.core.tree import ROOT_VARIABLES, choose_root

_VALID_FRAMINGS = ("raw", "codex-json")

EQUIVALENT = "equivalent"
DEFICIENT = "deficient"
UNRELATED = "unrelated"

# What the hook runs after the interpreter: the installed package's wake.
# -P keeps the current directory (a project, or the memory tree itself) off
# the front of the import path, so no file there can stand in for smriti's.
WAKE_MODULE = "smriti.wake"
WAKE_ARGS = ("-P", "-m", WAKE_MODULE)


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
    """Command string a harness runs at SessionStart: ``<python> -P -m smriti.wake``.

    ``style="sh"`` emits the POSIX form (Claude Code runs hook commands
    under a POSIX shell on every platform, including Windows).
    ``style="powershell-encoded"`` emits
    ``powershell.exe -NoProfile -NonInteractive -EncodedCommand <b64>``
    — required for Codex on Windows, which runs hooks under PowerShell,
    NOT bash; a sh-form command string does not survive there.

    Either form runs ``interpreter`` (default: the python running the
    installer, :func:`installing_interpreter`) by its path, not a
    ``python`` looked up on the harness's PATH.
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
            f'{_sh_interpreter(python)} {" ".join(WAKE_ARGS)}'
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
        parts.append(f"{_ps_interpreter(python)} {' '.join(WAKE_ARGS)}")
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

    # An earlier install's hook: the $HOME-relative posix path to the tree's
    # wake.py it runs. "" for a hook that runs the module.
    wake_path: str = ""
    env: dict[str, str] = field(default_factory=dict)
    interpreter: str = ""  # what runs it: a path, or a bare name looked up on PATH
    flags: tuple[str, ...] = ()  # the interpreter's options before ``-m smriti.wake``


ENCODED_RE = re.compile(r"-EncodedCommand\s+([A-Za-z0-9+/=]+)", re.IGNORECASE)
_PS_ENV_LITERAL_RE = re.compile(r"\$env:(\w+)\s*=\s*'([^']*)'")
_PS_ENV_JOINPATH_RE = re.compile(
    r"\$env:(\w+)\s*=\s*\(Join-Path \$HOME '([^']*)'\)"
)
# What follows the interpreter: options (``-X``/``-W`` with their values), then
# ``-m smriti.wake``; or, in an earlier install's hook, the tree's wake.py.
_FLAGS = r"(?P<flags>(?:-[XW]\s+\S+\s+|-[A-Za-z]+\s+)*)"
_MODULE = r"-m\s+smriti\.wake(?![\w.])"
# PowerShell's interpreter: the call operator and a single-quoted path (the
# generated form), or a bare python (earlier installs).
_PS_WAKE_RE = re.compile(
    r"(?:&\s*'(?P<path>(?:[^']|'')*)'|(?P<bare>python3?(?:\.exe)?))\s+"
    rf"(?:{_FLAGS}{_MODULE}|\(Join-Path \$HOME '(?P<wake>[^']*wake\.py)'\))"
)
_SH_ENV_RE = re.compile(r'\b(SMRITI_\w+|NARADA_ROOT)=("([^"]*)"|\'([^\']*)\'|(\S+))')
# The POSIX shell's interpreter, one word: single-quoted (the generated form;
# an embedded quote is '"'"'), double-quoted, or unquoted and naming a python
# (the bare python or python3 of earlier installs, or a path to one). Doctor
# finds the scripts hooks run with the same pattern.
SH_INTERPRETER = r"""(?:'[^']*'(?:"'"'[^']*')*|"[^"]*"|[^\s'";&|()<>]*python[\w.]*)"""
_SH_WAKE_RE = re.compile(
    rf'(?P<interp>{SH_INTERPRETER})\s+'
    rf'(?:{_FLAGS}{_MODULE}|"?\$HOME/(?P<wake>[^"\s]*wake\.py)"?)'
)


def _read_env(name: str) -> bool:
    """Whether a hook's assignment of *name* is one smriti reads."""
    return name.startswith("SMRITI_") or name in ROOT_VARIABLES


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


def hook_commands(hooks: object) -> list[str]:
    """Every command string under a hook config's list-shaped event entries.

    Skips non-event tables that share the ``hooks`` namespace (Codex keeps a
    ``[hooks.state]`` trust registry there) and any entry that is not
    dict-shaped.
    """
    if not isinstance(hooks, dict):
        return []
    return [
        str(hook.get("command", ""))
        for groups in hooks.values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, dict)
        for hook in group.get("hooks", [])
        if isinstance(hook, dict)
    ]


def runs_tree_wake(command: str, *, memory_root: Path, home: Path) -> bool:
    """Whether *command* runs ``<memory_root>/.smriti/wake.py``, the script earlier
    installs deployed, in any form: sh or PowerShell-encoded, by ``$HOME``, ``~`` or
    a written-out path. A command that names the path at all counts."""
    texts = [command]
    encoded = ENCODED_RE.search(command)
    if encoded:
        try:
            texts.append(base64.b64decode(encoded.group(1)).decode("utf-16-le"))
        except (ValueError, UnicodeDecodeError):
            pass
    names = [(memory_root / ".smriti" / "wake.py").as_posix().lower()]
    try:
        names.append(f"{memory_root.relative_to(home).as_posix()}/.smriti/wake.py".lower())
    except ValueError:
        pass
    return any(name in text.replace("\\", "/").lower() for text in texts for name in names)


def _flags(found: str | None) -> tuple[str, ...]:
    return tuple(found.split()) if found else ()


def parse_wake_command(command: str) -> WakeHookSpec | None:
    """Extract wake semantics from an sh or PowerShell-encoded command.

    Returns None when the command runs neither ``-m smriti.wake`` nor a
    ``wake.py`` (i.e. it is not a wake hook of any lineage — UNRELATED
    territory).
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
            if _read_env(name):
                env[name] = value
        for name, rel in _PS_ENV_JOINPATH_RE.findall(script):
            if _read_env(name):
                env[name] = f"$HOME/{_normalize(rel)}"
        return WakeHookSpec(wake_path=_normalize(wake.group("wake") or ""), env=env,
                            interpreter=interpreter, flags=_flags(wake.group("flags")))

    wake = _SH_WAKE_RE.search(command)
    if not wake:
        return None
    env = {}
    for m in _SH_ENV_RE.finditer(command):
        env[m.group(1)] = m.group(3) or m.group(4) or m.group(5) or ""
    return WakeHookSpec(wake_path=_normalize(wake.group("wake") or ""), env=env,
                        interpreter=_sh_unquote(wake.group("interp")),
                        flags=_flags(wake.group("flags")))


# --- classification -------------------------------------------------------

_WAKE_ON = {"1", "full", "on", "true", "yes"}


def _expand_home(value: str, home: Path) -> str:
    """A root as a hook names it, ``$HOME`` or ``~`` at its start made *home*."""
    path = value.replace("\\", "/")
    for prefix in ("$HOME/", "${HOME}/", "~/"):
        if path.startswith(prefix):
            return f"{home.as_posix()}/{path[len(prefix):]}"
    return path


def _path_key(path: Path) -> str:
    """*path* for comparison: resolved, in either slash and, on Windows, any case."""
    return os.path.normcase(str(path.resolve()))


def woken_root(env: dict[str, str], home: Path) -> tuple[Path, str]:
    """The tree a module-form hook with the assignments *env* wakes, and the rule
    that chose it (:func:`smriti.core.tree.choose_root`).

    The hook's own SMRITI_ROOT and NARADA_ROOT, over the environment it runs
    in, which is taken to be the one this process sees.
    """
    merged = {name: os.environ.get(name, "") for name in ROOT_VARIABLES}
    for name in ROOT_VARIABLES:
        if name in env:
            merged[name] = _expand_home(env[name], home)
    return choose_root(merged, home)


def _keeps_cwd_off_the_path(flags: tuple[str, ...]) -> bool:
    """Whether the interpreter's options include ``-P`` (or ``-I``, which implies it)."""
    return any(f.startswith("-") and not f.startswith("--") and ("P" in f or "I" in f)
               for f in flags)


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
    So is an earlier install's hook for this tree, which runs the tree's
    ``wake.py`` instead of the package's module.
    """
    spec = parse_wake_command(command)
    if spec is None:
        return UNRELATED, "does not run smriti's wake"

    memory_rel = memory_root.relative_to(home).as_posix()
    gaps: list[str] = []
    if spec.wake_path:
        expected_wake = _normalize(f"{memory_rel}/.smriti/wake.py")
        if spec.wake_path != expected_wake:
            return UNRELATED, f"targets {spec.wake_path}, not {expected_wake}"
        gaps.append(f"runs $HOME/{expected_wake} from the memory tree, "
                    f"not {WAKE_MODULE} from the installed package")
    else:
        woken, rule = woken_root(spec.env, home)
        if _path_key(woken) != _path_key(memory_root):
            named = spec.env.get("SMRITI_ROOT", "")
            shown = named or f"{woken.as_posix()} ({rule}: the hook sets no SMRITI_ROOT)"
            return UNRELATED, f"wakes {shown}, not $HOME/{memory_rel}"
        if not _keeps_cwd_off_the_path(spec.flags):
            gaps.append("runs without -P, so the current directory comes first on its "
                        "import path")
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
    "WAKE_ARGS",
    "WAKE_MODULE",
    "WakeHookSpec",
    "installing_interpreter",
    "make_wake_hook_command",
    "parse_wake_command",
    "classify_wake_hook",
    "hook_commands",
    "runs_tree_wake",
    "woken_root",
]
