"""Where the embedding service keeps its key, discovery file, lock and log.

A private per-user directory, never inside the memory root:

- ``%LOCALAPPDATA%\\smriti\\run`` on Windows, private to the user by the
  profile's ACLs;
- ``$XDG_RUNTIME_DIR/smriti``, or else ``~/.cache/smriti/run``, elsewhere:
  created 0700, and refused when another user owns it or others can write
  to it.

``SMRITI_RUNTIME_DIR`` overrides it (tests use temporary directories).

One key serves every memory root of the user: 32 random bytes, created
once by an exclusive create (mode 0600) and never replaced. Each memory
root has its own service, so its own lock, discovery file, log and spawn
stamp, named by a digest of the root.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from smriti.embed.identity import EmbeddingIdentity
from smriti.embed.protocol import PROTOCOL

log = logging.getLogger(__name__)

RUNTIME_ENV = "SMRITI_RUNTIME_DIR"
_KEY_NAME = "embed.key"
_KEY_TEXT = re.compile(r"[0-9a-f]{64}")


class RuntimeDirError(PermissionError):
    """The runtime directory, or the key in it, isn't safe to use."""


def default_runtime_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "smriti" / "run"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "smriti"
    return Path.home() / ".cache" / "smriti" / "run"


def private_dir_problem(st: os.stat_result, uid: int) -> str | None:
    """Why a directory with this ``lstat`` can't hold the key, or None when it can."""
    if stat.S_ISLNK(st.st_mode):
        return "it is a symbolic link"
    if not stat.S_ISDIR(st.st_mode):
        return "it is not a directory"
    if st.st_uid != uid:
        return f"it is owned by uid {st.st_uid}, not by this user (uid {uid})"
    if st.st_mode & 0o022:
        return f"other users can write to it (mode {stat.S_IMODE(st.st_mode):o})"
    return None


def _verify_private_dir(path: Path) -> None:
    if sys.platform == "win32":
        return  # the profile's ACLs make %LOCALAPPDATA% the user's own (see SECURITY.md)
    st = os.lstat(path)
    problem = private_dir_problem(st, os.getuid())
    if problem:
        raise RuntimeDirError(f"refusing the runtime directory {path}: {problem}")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)  # ours, and not writable by others: close it to them too


def runtime_dir(*, create: bool = True) -> Path:
    """The runtime directory, created and checked (only checked when ``create`` is False)."""
    env = os.environ.get(RUNTIME_ENV, "").strip()
    path = Path(env).expanduser() if env else default_runtime_dir()
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.path.lexists(path):
        _verify_private_dir(path)
    return path


def _read_text(path: Path) -> str:
    if sys.platform != "win32":
        return path.read_text(encoding="ascii")
    for attempt in range(40):
        try:
            return path.read_text(encoding="ascii")
        except PermissionError:
            # A sharing violation while another process renames the key into place.
            time.sleep(0.01 * (attempt + 1))
    return path.read_text(encoding="ascii")


def _read_key(path: Path) -> bytes:
    if sys.platform != "win32":
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise RuntimeDirError(f"refusing the key {path}: it must be a file only this "
                                  "user can read")
    text = _read_text(path).strip()
    if not _KEY_TEXT.fullmatch(text):
        raise RuntimeDirError(f"the key {path} is malformed; delete it while no embedding "
                              "service runs, and the next one makes a new key")
    return bytes.fromhex(text)


def load_key(runtime: Path, *, create: bool = True) -> bytes:
    """The per-user key, made first if there is none (atomically: never half-written)."""
    path = runtime / _KEY_NAME
    try:
        return _read_key(path)
    except FileNotFoundError:
        if not create:
            raise
    tmp = runtime / f".{_KEY_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, secrets.token_hex(32).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        # Both refuse an existing destination, so the first maker's key stands.
        if sys.platform == "win32":
            os.rename(tmp, path)
        else:
            os.link(tmp, path)
    except FileExistsError:
        pass
    except PermissionError:
        # Windows, while another process's key is landing there: theirs stands.
        if not path.exists():
            raise
    finally:
        tmp.unlink(missing_ok=True)
    return _read_key(path)


@dataclass(frozen=True)
class ServicePaths:
    """The files of one memory root's service."""

    root: Path
    discovery: Path  # port, pid, instance, state: written only by the service holding the lock
    lock: Path  # held by the service for its lifetime
    log: Path
    spawn: Path  # touched by the last client to start a service (start throttle)


def service_paths(runtime: Path, root: Path) -> ServicePaths:
    resolved = root.resolve()
    digest = hashlib.sha1(os.path.normcase(str(resolved)).encode("utf-8")).hexdigest()[:16]
    stem = f"embed-{digest}"
    return ServicePaths(resolved, runtime / f"{stem}.json", runtime / f"{stem}.lock",
                        runtime / f"{stem}.log", runtime / f"{stem}.spawn")


@dataclass(frozen=True)
class Discovery:
    """What the service says about itself in its discovery file."""

    pid: int
    state: str  # "starting", "ready", "draining", "failed"
    instance: str
    port: int = 0
    proto: int = PROTOCOL
    root: str = ""
    identity: EmbeddingIdentity | None = None
    error: str = ""
    updated: float = 0.0  # wall-clock time of this state
    version: str = ""


def read_discovery(paths: ServicePaths) -> Discovery | None:
    try:
        data = json.loads(paths.discovery.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Discovery(
            pid=int(data["pid"]), state=str(data["state"]), instance=str(data["instance"]),
            port=int(data.get("port") or 0), proto=int(data.get("proto") or 0),
            root=str(data.get("root") or ""),
            identity=EmbeddingIdentity.from_dict(data.get("identity")),
            error=str(data.get("error") or ""), updated=float(data.get("updated") or 0.0),
            version=str(data.get("version") or ""))
    except (KeyError, TypeError, ValueError):
        return None


def write_discovery(paths: ServicePaths, d: Discovery) -> None:
    from dataclasses import asdict

    from smriti.core.files import write_atomic

    data = asdict(d)
    data["updated"] = d.updated or time.time()
    write_atomic(paths.discovery, json.dumps(data, indent=1))


def clear_discovery(paths: ServicePaths, instance: str) -> None:
    """Remove the discovery file if it is still this instance's."""
    current = read_discovery(paths)
    if current is not None and current.instance == instance:
        try:
            paths.discovery.unlink()
        except OSError as exc:
            log.warning("could not remove %s: %s", paths.discovery, exc)


def pid_alive(pid: int) -> bool:
    from smriti.daylog.lock import _pid_alive

    return _pid_alive(pid)
