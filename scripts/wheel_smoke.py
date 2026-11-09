"""wheel_smoke.py — install a built wheel in a fresh venv and run smriti from it.

What CI's wheel job runs, and what to run before a release::

    python -m pip wheel . --no-deps -w dist
    python scripts/wheel_smoke.py dist/smriti-<version>-py3-none-any.whl --work <new dir>

It needs only the standard library. It creates a venv under ``--work``,
installs the wheel there with its ``read`` extra, and runs every smriti
command with ``--work`` as the working directory, without PYTHONPATH and
against a home, a memory root and a runtime directory under ``--work``, so
nothing from a checkout can stand in for the wheel:

1. the package imports from the venv and reports the wheel's version;
2. ``python -m smriti.install --harness none``, the privacy questions
   answered by flags, with a projects root holding one project: the
   memory template, the deployed scripts, the mirror and the recorded
   projects root are checked;
3. ``smriti write``, ``smriti read`` and ``smriti status``, with the stub
   embedder in-process (``SMRITI_EMBED_PROVIDER=stub``,
   ``SMRITI_EMBED_SERVICE=0``);
4. one ``smriti nightly`` (no witness) that captures a planted Claude Code
   transcript and digests it with the canned ``test_stub`` LLM, allowed by
   ``SMRITI_ALLOW_TEST_STUB=1``;
5. ``smriti backup`` in the memory root made a git repository (skipped
   without git): the package's backup commits it.

It stops at the first failure, printing the command and its output, and
exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import venv
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOTE = "Sam keeps the spare awls in the green tin beside the press."


class SmokeFailure(Exception):
    pass


def bin_dir(env_dir: Path) -> Path:
    return env_dir / ("Scripts" if sys.platform == "win32" else "bin")


def run(cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: float = 600) -> str:
    print(f"$ {' '.join(cmd)}", flush=True)
    done = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    output = done.stdout + done.stderr
    if done.returncode != 0:
        raise SmokeFailure(f"exit {done.returncode}: {' '.join(cmd)}\n{output}")
    return output


def check(condition: bool, what: str, detail: str = "") -> None:
    if not condition:
        raise SmokeFailure(f"{what}\n{detail}".rstrip())
    print(f"  ok: {what}", flush=True)


def smoke_env(work: Path, home: Path, root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SMRITI_") and k not in ("PYTHONPATH", "PYTHONHOME",
                                                        "VIRTUAL_ENV", "NARADA_ROOT")}
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "SMRITI_ROOT": str(root),
        "SMRITI_RUNTIME_DIR": str(work / "run"),
        "SMRITI_EMBED_PROVIDER": "stub",
        "SMRITI_EMBED_SERVICE": "0",
        "SMRITI_ALLOW_TEST_STUB": "1",
        "SMRITI_NIGHTLY_PROVIDER": "test_stub",
        "SMRITI_NIGHTLY_FALLBACK": "",
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def plant_transcript(home: Path, project_dir: Path) -> str:
    """A two-turn Claude Code session from a few minutes ago; returns its local day."""
    now = datetime.now(timezone.utc) - timedelta(minutes=5)
    folder = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project_dir))
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    for offset, (kind, text) in enumerate([
            ("user", "Ari, where did we put the spare awls?"),
            ("assistant", "In the green tin beside the press, Sam.")]):
        stamp = (now + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        content: object = text if kind == "user" else [{"type": "text", "text": text}]
        records.append({"type": kind, "timestamp": stamp, "cwd": str(project_dir),
                        "sessionId": "smoke-session",
                        "message": {"role": kind, "content": content}})
    (folder / "smoke-session.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return now.astimezone().date().isoformat()


def smoke(wheel: Path, work: Path, extras: str) -> None:
    home, venv_dir = work / "home", work / "venv"
    root, projects = home / ".ari", work / "projects"
    for path in (home, projects / "kite" / ".ai" / "knowledge"):
        path.mkdir(parents=True, exist_ok=True)
    env = smoke_env(work, home, root)
    version = wheel.name.split("-")[1]

    print(f"== venv at {venv_dir}", flush=True)
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    python = str(bin_dir(venv_dir) / ("python.exe" if sys.platform == "win32" else "python"))
    smriti = str(bin_dir(venv_dir) / ("smriti.exe" if sys.platform == "win32" else "smriti"))
    target = f"{wheel.resolve()}[{extras}]" if extras else str(wheel.resolve())
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "-q", target],
        cwd=work, env=env, timeout=1800)

    print("== the package is the wheel's", flush=True)
    where = json.loads(run([python, "-c", "import json, smriti; print(json.dumps("
                            "[smriti.__file__, smriti.__version__]))"], cwd=work, env=env))
    package = Path(where[0]).resolve()
    check(package.is_relative_to(venv_dir.resolve()), f"smriti imports from the venv ({package})")
    check(where[1] == version, f"smriti.__version__ is the wheel's ({version})", str(where))
    out = run([smriti, "--version"], cwd=work, env=env)
    check(out.strip() == f"smriti {version}", "smriti --version", out)

    print("== install", flush=True)
    out = run([python, "-m", "smriti.install", "--harness", "none", "--memory-root", str(root),
               "--projects-root", str(projects), "--capture", "--remote-is-private"],
              cwd=work, env=env)
    print(out)
    check((root / "identity.md").is_file(), "the memory template was copied")
    check((root / "journal").is_dir(), "its empty directories were made")
    for name in ("wake.py", "narada-p.sh"):
        check((root / ".smriti" / name).is_file(), f".smriti/{name} was deployed")
    check(not (root / ".smriti" / "backup.py").exists(), ".smriti/backup.py was not deployed")
    record = json.loads((root / ".smriti" / "install.json").read_text(encoding="utf-8"))
    check(Path(record["projects_root"]) == projects, "the projects root was recorded", str(record))
    check((root / "mirrors" / "kite" / "ai").exists(), "the project's mirror was linked")
    daylog = json.loads((root / ".smriti" / "daylog.json").read_text(encoding="utf-8"))
    check(daylog.get("capture") is True, "capture was turned on by --capture", str(daylog))
    check(out.rstrip().splitlines()[-1].startswith("  (the backup and the nightly run")
          or "\nprivacy:\n" in out, "the privacy summary was printed", out)
    out = run([python, "-m", "smriti.install", "--harness", "none", "--memory-root", str(root)],
              cwd=work, env=env)
    check("[wake] wake.py up to date" in out, "a rerun finds the scripts up to date", out)

    print("== write, read, status", flush=True)
    out = run([smriti, "write", NOTE, "--branch", "notes", "--title", "Where the awls are"],
              cwd=work, env=env)
    written = sorted((root / "notes").rglob("*.md"))
    check(any("green tin" in p.read_text(encoding="utf-8") for p in written),
          "smriti write wrote the note", out)
    out = run([smriti, "read", "spare awls green tin", "--no-rerank"], cwd=work, env=env)
    check("green tin" in out, "smriti read finds it", out)
    out = run([smriti, "status"], cwd=work, env=env)
    check(str(root) in out, "smriti status names the memory root", out)

    print("== nightly", flush=True)
    day = plant_transcript(home, projects / "kite")
    out = run([smriti, "nightly", "--no-witness"], cwd=work, env=env)
    print(out)
    digest = root / "log" / day[:4] / day[5:7] / f"{day[8:]}-digest.md"
    check(digest.is_file(), f"the nightly digested {day}", out)
    check("test_stub: a canned answer" in digest.read_text(encoding="utf-8"),
          "with the canned LLM")
    status = json.loads((root / "log" / ".nightly-status.json").read_text(encoding="utf-8"))
    check(status.get("ok") is True, "the night is ok", json.dumps(status, indent=1))

    print("== backup", flush=True)
    if shutil.which("git") is None:
        print("  skipped: git is not installed", flush=True)
    else:
        git_env = {**env, "GIT_AUTHOR_NAME": "Ari", "GIT_AUTHOR_EMAIL": "ari@example.com",
                   "GIT_COMMITTER_NAME": "Ari", "GIT_COMMITTER_EMAIL": "ari@example.com"}
        run(["git", "init", "-q"], cwd=root, env=git_env)
        out = run([smriti, "backup"], cwd=work, env=git_env)
        check("Backup (manual): commit ok; push not asked" in out,
              "smriti backup committed the tree", out)
        out = run(["git", "log", "-1", "--format=%s"], cwd=root, env=git_env)
        check(out.strip() == "snapshot: manual", "with the backup's message", out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wheel", type=Path, help="the built smriti wheel")
    parser.add_argument("--work", type=Path, required=True,
                        help="an empty directory for the venv, home and memory root")
    parser.add_argument("--extras", default="read", help="extras to install (default: read)")
    args = parser.parse_args(argv)
    work = args.work.resolve()
    if work.exists() and any(work.iterdir()):
        parser.error(f"--work must be empty or absent: {work}")
    work.mkdir(parents=True, exist_ok=True)
    try:
        smoke(args.wheel, work, args.extras)
    except SmokeFailure as exc:
        print(f"\nSMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    print("\nwheel smoke: all steps passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
