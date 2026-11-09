"""Tests for src/smriti/integrations/common/hook_model.py.

The canonical wake-hook model is shared by installers and doctor —
generator and classifier live together precisely so these tests can
pin the roundtrip: whatever any style generates must classify
EQUIVALENT, and the live PowerShell form found on Windows machines
must never be misread as broken (the 2026-07-16 audit's false-FAIL).
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from smriti.integrations.common.hook_model import (
    DEFICIENT,
    EQUIVALENT,
    UNRELATED,
    classify_wake_hook,
    make_wake_hook_command,
    parse_wake_command,
)

HOME = Path("/home/u")
ROOT = HOME / ".narada"


WEIRD_INTERPRETERS = [
    r"C:\Program Files\Python 3.12\python.exe",
    r"C:\Users\O'Neil\py\python.exe",
    "/home/o'neil/py env/bin/python3",
    "/opt/$HOME/`x`/bin/python",
    '/opt/a "b"/python',
    "python",
]


def _script(cmd: str) -> str:
    """The PowerShell script inside an ``-EncodedCommand``."""
    return base64.b64decode(cmd.rsplit(" ", 1)[1]).decode("utf-16-le")


def _classify(cmd: str, framing: str = "raw",
              interpreter: str | None = None) -> tuple[str, str]:
    return classify_wake_hook(cmd, memory_root=ROOT, home=HOME, framing=framing,
                              interpreter=interpreter)


# The exact command shape found live in ~/.codex/config.toml on the
# Windows machine during the 2026-07-16 audit (regenerated here so the
# test is entity-name-agnostic).
def _live_style_ps_command() -> str:
    script = (
        "$ProgressPreference='SilentlyContinue'; "
        "$ErrorActionPreference='Stop'; "
        "$env:SMRITI_WAKE='1'; "
        "$env:SMRITI_ROOT=(Join-Path $HOME '.narada'); "
        "$env:SMRITI_WAKE_AUDIENCE='coding'; "
        "$env:SMRITI_WAKE_FRAMING='codex-json'; "
        "python (Join-Path $HOME '.narada/.smriti/wake.py')"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


class TestGeneration:
    def test_sh_form_shape(self):
        cmd = make_wake_hook_command(ROOT, home=HOME, framing="raw", style="sh")
        assert "SMRITI_WAKE=1" in cmd
        assert 'SMRITI_ROOT="$HOME/.narada"' in cmd
        assert "\\" not in cmd

    def test_powershell_form_is_encoded(self):
        cmd = make_wake_hook_command(
            ROOT, home=HOME, framing="codex-json", style="powershell-encoded"
        )
        assert cmd.startswith("powershell.exe -NoProfile -NonInteractive -EncodedCommand ")
        # No raw env assignments leak into the outer command.
        assert "SMRITI_WAKE=1" not in cmd

    @pytest.mark.parametrize("style", ["sh", "powershell-encoded"])
    def test_the_installing_interpreter_is_named(self, style):
        """H15.1: the hook ran a bare ``python`` from the harness's PATH."""
        cmd = make_wake_hook_command(ROOT, home=HOME, style=style)
        spec = parse_wake_command(cmd)
        assert spec is not None
        assert spec.interpreter.replace("\\", "/") == sys.executable.replace("\\", "/")
        script = _script(cmd) if style != "sh" else cmd
        assert "python (" not in script and 'python "' not in script

    @pytest.mark.parametrize("style", ["sh", "powershell-encoded"])
    @pytest.mark.parametrize("interpreter", WEIRD_INTERPRETERS)
    def test_any_interpreter_path_round_trips(self, style, interpreter):
        cmd = make_wake_hook_command(ROOT, home=HOME, style=style, interpreter=interpreter)
        spec = parse_wake_command(cmd)
        assert spec is not None
        expected = interpreter
        if style == "sh" and interpreter.startswith("C:"):
            expected = interpreter.replace("\\", "/")  # sh holds no backslash
        assert spec.interpreter == expected
        assert spec.wake_path == ".narada/.smriti/wake.py"
        assert _classify(cmd, interpreter=interpreter)[0] == EQUIVALENT

    def test_sh_form_writes_a_windows_interpreter_with_forward_slashes(self):
        cmd = make_wake_hook_command(ROOT, home=HOME,
                                     interpreter=r"C:\Program Files\Python 3.12\python.exe")
        assert "'C:/Program Files/Python 3.12/python.exe' \"$HOME/.narada/.smriti/wake.py\"" in cmd
        assert "\\" not in cmd

    def test_powershell_form_calls_the_interpreter_by_path(self):
        cmd = make_wake_hook_command(ROOT, home=HOME, style="powershell-encoded",
                                     interpreter=r"C:\Users\O'Neil\py\python.exe")
        assert ("& 'C:\\Users\\O''Neil\\py\\python.exe' "
                "(Join-Path $HOME '.narada/.smriti/wake.py')") in _script(cmd)

    def test_unknown_style_raises(self):
        with pytest.raises(ValueError, match="style"):
            make_wake_hook_command(ROOT, home=HOME, style="cmd-exe")

    def test_unknown_framing_raises(self):
        with pytest.raises(ValueError, match="framing"):
            make_wake_hook_command(ROOT, home=HOME, framing="bogus")


class TestParse:
    @pytest.mark.parametrize("style", ["sh", "powershell-encoded"])
    @pytest.mark.parametrize("framing", ["raw", "codex-json"])
    def test_roundtrip_parse(self, style, framing):
        cmd = make_wake_hook_command(ROOT, home=HOME, framing=framing, style=style)
        spec = parse_wake_command(cmd)
        assert spec is not None
        assert spec.wake_path == ".narada/.smriti/wake.py"
        assert spec.env["SMRITI_WAKE"] == "1"
        assert spec.env.get("SMRITI_WAKE_AUDIENCE") == "coding"
        if framing == "codex-json":
            assert spec.env["SMRITI_WAKE_FRAMING"] == "codex-json"

    def test_non_wake_command_returns_none(self):
        assert parse_wake_command("python ~/.claude/hooks/recall.py") is None
        assert parse_wake_command("echo hello") is None

    def test_garbage_encoded_returns_none(self):
        assert parse_wake_command("powershell.exe -EncodedCommand AAAA") is None


class TestClassify:
    @pytest.mark.parametrize("style", ["sh", "powershell-encoded"])
    @pytest.mark.parametrize("framing", ["raw", "codex-json"])
    def test_generated_commands_classify_equivalent(self, style, framing):
        cmd = make_wake_hook_command(ROOT, home=HOME, framing=framing, style=style)
        verdict, detail = _classify(cmd, framing=framing)
        assert verdict == EQUIVALENT, detail

    def test_live_windows_ps_hook_is_read_as_written(self):
        """The 2026-07-16 audit false-FAIL: the PowerShell encoding must not
        be misread as broken semantics. Since H15.1 the hook must also name
        the installing interpreter, so this one, with its bare ``python``, is
        equivalent only against ``python`` (it was equivalent outright)."""
        verdict, detail = _classify(_live_style_ps_command(), framing="codex-json",
                                    interpreter="python")
        assert verdict == EQUIVALENT, detail

    @pytest.mark.parametrize("cmd,framing", [
        (_live_style_ps_command(), "codex-json"),
        ('SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" python "$HOME/.narada/.smriti/wake.py"',
         "raw"),
        ('SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" python3 "$HOME/.narada/.smriti/wake.py"',
         "raw"),
    ])
    def test_a_hook_running_python_from_path_is_deficient(self, cmd, framing):
        """H15.1: a bare python is whichever the harness's PATH finds first,
        which may not have smriti. Deficient, so a reinstall pins it."""
        verdict, detail = _classify(cmd, framing=framing, interpreter="/opt/ari/bin/python3")
        assert verdict == DEFICIENT
        assert "from PATH, not /opt/ari/bin/python3" in detail

    def test_another_interpreter_is_deficient(self):
        cmd = make_wake_hook_command(ROOT, home=HOME, interpreter="/usr/bin/python3")
        verdict, detail = _classify(cmd, interpreter="/opt/ari/bin/python3")
        assert verdict == DEFICIENT
        assert detail == "runs /usr/bin/python3, not /opt/ari/bin/python3"

    def test_a_windows_interpreter_matches_in_either_slash_and_case(self):
        cmd = make_wake_hook_command(ROOT, home=HOME, interpreter=r"C:\Py\python.exe")
        verdict, detail = _classify(cmd, interpreter="c:/py/PYTHON.EXE")
        assert verdict == EQUIVALENT, detail

    def test_classification_defaults_to_the_running_interpreter(self):
        verdict, detail = _classify(make_wake_hook_command(ROOT, home=HOME))
        assert verdict == EQUIVALENT, detail
        verdict, _ = _classify(make_wake_hook_command(ROOT, home=HOME, interpreter="python"))
        assert verdict == DEFICIENT

    def test_missing_framing_is_deficient_for_codex(self):
        legacy = (
            'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" '
            'python "$HOME/.narada/.smriti/wake.py"'
        )
        verdict, detail = _classify(legacy, framing="codex-json")
        assert verdict == DEFICIENT
        assert "framing" in detail

    def test_missing_audience_defaults_to_coding_equivalent(self):
        """The live Claude Code hook predates the audience var; the
        runner defaults to coding, so it is semantically equivalent."""
        legacy = (
            'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" '
            'python "$HOME/.narada/.smriti/wake.py"'
        )
        verdict, detail = _classify(legacy, framing="raw", interpreter="python")
        assert verdict == EQUIVALENT, detail

    def test_missing_smriti_wake_is_deficient(self):
        cmd = 'SMRITI_ROOT="$HOME/.narada" python "$HOME/.narada/.smriti/wake.py"'
        verdict, detail = _classify(cmd)
        assert verdict == DEFICIENT
        assert "SMRITI_WAKE" in detail

    def test_wrong_root_is_deficient(self):
        cmd = (
            'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.other" '
            'python "$HOME/.narada/.smriti/wake.py"'
        )
        verdict, detail = _classify(cmd)
        assert verdict == DEFICIENT
        assert "SMRITI_ROOT" in detail

    def test_other_entity_wake_is_unrelated(self):
        cmd = 'SMRITI_WAKE=1 python "$HOME/.tara/.smriti/wake.py"'
        verdict, _ = _classify(cmd)
        assert verdict == UNRELATED

    def test_non_wake_hook_is_unrelated(self):
        verdict, _ = _classify("python ~/.claude/hooks/associative_recall.py")
        assert verdict == UNRELATED


# --- running the commands -------------------------------------------------

LINK_NAME = "py env's $HOME"  # a space, a quote and a variable: all must stay literal


def _linked_interpreter(tmp_path: Path) -> str:
    """This interpreter, reached through a directory named :data:`LINK_NAME`
    (a junction on Windows, which needs no privilege; a symlink elsewhere).

    The link is to ``sys.prefix``, so a venv's interpreter still finds its
    ``pyvenv.cfg``. Skips where the link can't be made or run.
    """
    try:
        inside = Path(sys.executable).relative_to(sys.prefix)
    except ValueError:
        pytest.skip("the interpreter isn't under sys.prefix")
    link = tmp_path / LINK_NAME
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(sys.prefix, str(link))
        else:
            link.symlink_to(sys.prefix, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"can't link the interpreter's directory: {exc}")
    python = str(link / inside)
    if subprocess.run([python, "-c", "pass"], capture_output=True, timeout=60).returncode:
        pytest.skip("the interpreter doesn't run through a linked directory here")
    return python


def _fake_wake(home: Path) -> Path:
    """A wake.py at ``<home>/.ari/.smriti/`` that reports what ran it."""
    root = home / ".ari"
    (root / ".smriti").mkdir(parents=True)
    (root / ".smriti" / "wake.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'executable': sys.executable,\n"
        "                  'root': os.environ.get('SMRITI_ROOT'),\n"
        "                  'wake': os.environ.get('SMRITI_WAKE')}))\n",
        encoding="utf-8",
    )
    return root


def _ran(out: subprocess.CompletedProcess[str], root: Path, *, linked: bool = True) -> None:
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    if linked:  # reached through the link, as the interpreter reports itself
        assert LINK_NAME in report["executable"]
    assert Path(report["root"]) == root
    assert report["wake"] == "1"


def _wrapped_interpreter(tmp_path: Path) -> tuple[str, Path]:
    """A ``python`` in a directory named :data:`LINK_NAME` that leaves a mark
    and then runs this interpreter: proof the command ran that very path.

    (A macOS framework build reports its resolved path as ``sys.executable``
    whatever path started it, so the executable can't show the link there.)
    """
    folder = tmp_path / LINK_NAME
    folder.mkdir()
    mark = tmp_path / "ran-the-named-interpreter"
    wrapper = folder / "python"
    wrapper.write_text(
        f"#!/bin/sh\n: > {shlex.quote(str(mark))}\nexec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return str(wrapper), mark


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("sh") is None,
                    reason="the sh form is run by a POSIX shell")
def test_the_sh_form_runs_the_named_interpreter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = _fake_wake(home)
    python, mark = _wrapped_interpreter(tmp_path)
    cmd = make_wake_hook_command(root, home=home, interpreter=python)

    out = subprocess.run(["sh", "-c", cmd], env={**os.environ, "HOME": str(home)},
                         capture_output=True, text=True, timeout=60)

    _ran(out, root, linked=False)
    assert mark.exists()


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("powershell.exe") is None,
                    reason="the encoded form is run by Windows PowerShell")
def test_the_powershell_form_runs_the_named_interpreter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = _fake_wake(home)
    python = _linked_interpreter(tmp_path)
    cmd = make_wake_hook_command(root, home=home, framing="codex-json",
                                 style="powershell-encoded", interpreter=python)

    # PowerShell's $HOME is USERPROFILE.
    out = subprocess.run(cmd.split(" "), env={**os.environ, "USERPROFILE": str(home)},
                         capture_output=True, text=True, timeout=120)

    _ran(out, root)
