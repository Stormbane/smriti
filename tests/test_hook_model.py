"""Tests for src/smriti/integrations/common/hook_model.py.

The canonical wake-hook model is shared by installers and doctor —
generator and classifier live together precisely so these tests can
pin the roundtrip: whatever any style generates must classify
EQUIVALENT, and the live PowerShell form found on Windows machines
must never be misread (the 2026-07-16 audit's false-FAIL). The hook runs
the installed package's wake, ``<python> -P -m smriti.wake``; an earlier
install's hook, which ran the memory tree's ``wake.py``, is ours and
deficient, so a reinstall replaces it.
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
# test is entity-name-agnostic): an earlier install's, running the tree's wake.py.
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


# The Claude Code hook an earlier install wrote, with the installing interpreter.
def _earlier_sh_command(python: str = sys.executable) -> str:
    return (f'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" SMRITI_WAKE_AUDIENCE="coding" '
            f"'{python}' \"$HOME/.narada/.smriti/wake.py\"")


TREE_WAKE_GAP = ("runs $HOME/.narada/.smriti/wake.py from the memory tree, "
                 "not smriti.wake from the installed package")


class TestGeneration:
    def test_sh_form_shape(self):
        cmd = make_wake_hook_command(ROOT, home=HOME, framing="raw", style="sh")
        assert "SMRITI_WAKE=1" in cmd
        assert 'SMRITI_ROOT="$HOME/.narada"' in cmd
        assert cmd.endswith(" -P -m smriti.wake")
        assert "wake.py" not in cmd
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
        assert spec.wake_path == "" and spec.flags == ("-P",)
        assert _classify(cmd, interpreter=interpreter)[0] == EQUIVALENT

    def test_sh_form_writes_a_windows_interpreter_with_forward_slashes(self):
        cmd = make_wake_hook_command(ROOT, home=HOME,
                                     interpreter=r"C:\Program Files\Python 3.12\python.exe")
        assert cmd.endswith("'C:/Program Files/Python 3.12/python.exe' -P -m smriti.wake")
        assert "\\" not in cmd

    def test_powershell_form_calls_the_interpreter_by_path(self):
        cmd = make_wake_hook_command(ROOT, home=HOME, style="powershell-encoded",
                                     interpreter=r"C:\Users\O'Neil\py\python.exe")
        assert _script(cmd).endswith("; & 'C:\\Users\\O''Neil\\py\\python.exe' -P -m smriti.wake")

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
        assert spec.wake_path == "" and spec.flags == ("-P",)
        assert spec.env["SMRITI_WAKE"] == "1"
        assert spec.env["SMRITI_ROOT"] == "$HOME/.narada"
        assert spec.env.get("SMRITI_WAKE_AUDIENCE") == "coding"
        if framing == "codex-json":
            assert spec.env["SMRITI_WAKE_FRAMING"] == "codex-json"

    def test_non_wake_command_returns_none(self):
        assert parse_wake_command("python ~/.claude/hooks/recall.py") is None
        assert parse_wake_command("echo hello") is None
        assert parse_wake_command("python -m smriti.wakeup") is None
        assert parse_wake_command("python -m smriti.wake.other") is None

    @pytest.mark.parametrize("cmd", [_live_style_ps_command(), _earlier_sh_command()])
    def test_an_earlier_installs_hook_names_the_trees_wake_py(self, cmd):
        spec = parse_wake_command(cmd)
        assert spec is not None
        assert spec.wake_path == ".narada/.smriti/wake.py" and spec.flags == ()

    def test_the_interpreters_options_are_read(self):
        spec = parse_wake_command("SMRITI_WAKE=1 python -X utf8 -I -m smriti.wake")
        assert spec is not None
        assert spec.interpreter == "python" and spec.flags == ("-X", "utf8", "-I")

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
        be misread as broken semantics. Run by ``python``, this earlier
        install's hook lacks only the package's wake: it runs the tree's."""
        verdict, detail = _classify(_live_style_ps_command(), framing="codex-json",
                                    interpreter="python")
        assert (verdict, detail) == (DEFICIENT, TREE_WAKE_GAP)

    def test_an_earlier_installs_hook_for_this_tree_is_deficient(self):
        """Whatever else it gets right, it runs a script in the memory tree, which a
        write to the tree or a restore of it could change: a reinstall replaces it."""
        verdict, detail = _classify(_earlier_sh_command())
        assert (verdict, detail) == (DEFICIENT, TREE_WAKE_GAP)

    def test_a_hook_without_dash_p_is_deficient(self):
        cmd = make_wake_hook_command(ROOT, home=HOME).replace(" -P -m", " -m")
        verdict, detail = _classify(cmd)
        assert verdict == DEFICIENT
        assert detail == ("runs without -P, so the current directory comes first on its "
                          "import path")
        isolated = make_wake_hook_command(ROOT, home=HOME).replace(" -P -m", " -I -m")
        assert _classify(isolated)[0] == EQUIVALENT  # -I implies -P

    def test_a_module_hook_for_another_tree_is_unrelated(self):
        cmd = make_wake_hook_command(HOME / ".tara", home=HOME)
        verdict, detail = _classify(cmd)
        assert verdict == UNRELATED
        assert detail == "wakes $HOME/.tara, not $HOME/.narada"

    @pytest.mark.parametrize("root", [f"{HOME.as_posix()}/.narada", "~/.narada", ""])
    def test_a_module_hook_naming_this_tree_otherwise_is_ours(self, root):
        """An absolute or ~ path to this tree, or no SMRITI_ROOT (the default
        tree): ours, and brought to the canonical form, never a second hook."""
        env = f'SMRITI_ROOT="{root}" ' if root else ""
        cmd = f"SMRITI_WAKE=1 {env}'{sys.executable}' -P -m smriti.wake"
        verdict, detail = _classify(cmd)
        assert verdict == DEFICIENT
        assert detail == "SMRITI_ROOT != $HOME/.narada"

    @pytest.mark.parametrize("cmd,framing", [
        (_live_style_ps_command(), "codex-json"),
        ('SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" python "$HOME/.narada/.smriti/wake.py"',
         "raw"),
        ('SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" python3 "$HOME/.narada/.smriti/wake.py"',
         "raw"),
        ('SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" python -P -m smriti.wake', "raw"),
    ])
    def test_a_hook_running_python_from_path_is_deficient(self, cmd, framing):
        """A bare python is whichever the harness's PATH finds first, which may
        not have smriti. Deficient, so a reinstall pins it."""
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
        """A hook without the audience var: the runner defaults to coding, so it
        is semantically equivalent."""
        cmd = 'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" python -P -m smriti.wake'
        verdict, detail = _classify(cmd, framing="raw", interpreter="python")
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


def _fake_wake(tmp_path: Path) -> dict[str, str]:
    """A stand-in ``smriti.wake`` that reports what ran it; the environment that finds it.

    On PYTHONPATH, which ``-P`` leaves in place (it only keeps the current
    directory off the import path).
    """
    package = tmp_path / "fake" / "smriti" / "wake"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'executable': sys.executable,\n"
        "                  'root': os.environ.get('SMRITI_ROOT'),\n"
        "                  'wake': os.environ.get('SMRITI_WAKE'),\n"
        "                  'cwd_on_path': '' in sys.path or os.getcwd() in sys.path}))\n",
        encoding="utf-8",
    )
    return {**os.environ, "PYTHONPATH": str(tmp_path / "fake")}


def _ran(out: subprocess.CompletedProcess[str], root: Path, *, linked: bool = True) -> None:
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    if linked:  # reached through the link, as the interpreter reports itself
        assert LINK_NAME in report["executable"]
    assert Path(report["root"]) == root
    assert report["wake"] == "1"
    assert report["cwd_on_path"] is False  # -P


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
    root = home / ".ari"
    env = _fake_wake(tmp_path)
    python, mark = _wrapped_interpreter(tmp_path)
    cmd = make_wake_hook_command(root, home=home, interpreter=python)

    out = subprocess.run(["sh", "-c", cmd], env={**env, "HOME": str(home)},
                         capture_output=True, text=True, timeout=60)

    _ran(out, root, linked=False)
    assert mark.exists()


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("powershell.exe") is None,
                    reason="the encoded form is run by Windows PowerShell")
def test_the_powershell_form_runs_the_named_interpreter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".ari"
    env = _fake_wake(tmp_path)
    python = _linked_interpreter(tmp_path)
    cmd = make_wake_hook_command(root, home=home, framing="codex-json",
                                 style="powershell-encoded", interpreter=python)

    # PowerShell's $HOME is USERPROFILE.
    out = subprocess.run(cmd.split(" "), env={**env, "USERPROFILE": str(home)},
                         capture_output=True, text=True, timeout=120)

    _ran(out, root)
