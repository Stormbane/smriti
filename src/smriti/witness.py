"""Witness — tamper-evidence for the memory tree.

Each night, once the tree is committed, a small manifest naming HEAD is
written under ``witness/`` and timestamped with OpenTimestamps: its
SHA-256 goes to several independent public calendars and, within hours,
is anchored in a Bitcoin block. Only a hash leaves the machine.

Nothing on this machine is out of reach of an agent with a shell, the
agent whose memory this is included. The anchor is. Rewrite the tree's
history and earlier manifests stop naming ancestors of HEAD;
``smriti witness verify`` reports the first night that no longer holds.
That says *that* and *since when*, not *what* — the old commits, or the
GitHub copy, answer what.

Optional dependency: the ``witness`` extra (``opentimestamps``). Without
it the nightly step fails visibly rather than skipping quietly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Independent operators, so no single calendar can withhold a stamp.
CALENDARS = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
    "https://ots.btc.catallaxy.com",
)
# A pending attestation names the calendar that will complete it. Only
# these are contacted on upgrade, so a tampered proof can't steer us.
_UPGRADE_CALENDARS = frozenset({
    "https://alice.btc.calendar.opentimestamps.org",
    "https://bob.btc.calendar.opentimestamps.org",
    "https://finney.calendar.eternitywall.com",
    "https://btc.calendar.catallaxy.com",
})
_MIN_CALENDARS = 2
_STALE_LOCK_S = 15 * 60
_STALE_HEAD_S = 26 * 3600
# Two explorers run by different people, checked in turn.
_EXPLORERS = ("https://mempool.space/api", "https://blockstream.info/api")
_TIMEOUT_S = 15


def _git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )


def clear_stale_lock(root: Path, *, max_age_s: float = _STALE_LOCK_S) -> bool:
    """Remove a git ``index.lock`` left behind by a killed process.

    Memory commits take seconds, so a lock this old belongs to a git that
    died mid-commit. Left alone it silently blocks every later backup: one
    from 2026-09-07 did, for six weeks.
    """
    lock = root / ".git" / "index.lock"
    try:
        age = time.time() - lock.stat().st_mtime
    except FileNotFoundError:
        return False
    if age < max_age_s:
        return False
    lock.unlink()
    log.warning("removed stale %s (%.0f min old)", lock, age / 60)
    return True


def _commit(root: Path, tag: str, *, push: bool = False) -> None:
    from smriti.backup import trigger

    trigger(tag, push=push, root=root)


def _manifest_path(root: Path, night: str) -> Path:
    return root / "witness" / night[:4] / f"{night}.txt"


def _normalized(data: bytes) -> bytes:
    # Line endings must not decide whether a proof verifies.
    return data.replace(b"\r\n", b"\n")


def write_manifest(root: Path, night: str, *, now: datetime | None = None) -> Path:
    """Write the night's manifest: which commit the tree stood at, and when."""
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").stdout.strip()
    if not head:
        raise RuntimeError("git rev-parse HEAD failed — empty or broken repo")
    stamped_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    text = (
        "smriti witness v1\n"
        f"night: {night}\n"
        f"stamped_at: {stamped_at}\n"
        f"head: {head}\n"
        f"tree: {tree}\n"
    )
    path = _manifest_path(root, night)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def read_manifest(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in _normalized(path.read_bytes()).decode("utf-8").splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            fields[key] = value
    return fields


def stamp(path: Path, *, calendars: tuple[str, ...] = CALENDARS) -> int:
    """Timestamp ``path`` beside it as ``<path>.ots``; returns calendars reached."""
    from opentimestamps.calendar import RemoteCalendar  # type: ignore[import-untyped]
    from opentimestamps.core.op import OpAppend, OpSHA256  # type: ignore[import-untyped]
    from opentimestamps.core.timestamp import (  # type: ignore[import-untyped]
        DetachedTimestampFile,
        Timestamp,
    )

    digest = hashlib.sha256(_normalized(path.read_bytes())).digest()
    detached = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
    # The nonce keeps calendars from learning the file's own hash.
    nonced = detached.timestamp.ops.add(OpAppend(os.urandom(16)))
    commitment = nonced.ops.add(OpSHA256())
    reached = 0
    for url in calendars:
        try:
            commitment.merge(RemoteCalendar(url).submit(commitment.msg, timeout=_TIMEOUT_S))
            reached += 1
        except Exception as exc:  # noqa: BLE001 — one calendar down is fine
            log.warning("calendar %s: %s", url, exc)
    if reached:
        _save(detached, path.with_name(path.name + ".ots"))
    return reached


def _save(detached: Any, path: Path) -> None:
    from opentimestamps.core.serialize import (  # type: ignore[import-untyped]
        StreamSerializationContext,
    )

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        detached.serialize(StreamSerializationContext(fh))
    tmp.replace(path)


def _load(path: Path) -> Any:
    from opentimestamps.core.serialize import (
        StreamDeserializationContext,
    )
    from opentimestamps.core.timestamp import (
        DetachedTimestampFile,
    )

    with path.open("rb") as fh:
        return DetachedTimestampFile.deserialize(StreamDeserializationContext(fh))


def _directly_attested(stamp_: Any) -> Iterator[Any]:
    if stamp_.attestations:
        yield stamp_
    else:
        for sub in stamp_.ops.values():
            yield from _directly_attested(sub)


def _bitcoin_attestations(detached: Any) -> list[tuple[int, bytes]]:
    from opentimestamps.core.notary import (  # type: ignore[import-untyped]
        BitcoinBlockHeaderAttestation,
    )

    return [
        (int(att.height), bytes(msg))
        for msg, att in detached.timestamp.all_attestations()
        if isinstance(att, BitcoinBlockHeaderAttestation)
    ]


def upgrade(path: Path) -> bool:
    """Fetch Bitcoin attestations for a pending proof; True once it has one."""
    from opentimestamps.calendar import RemoteCalendar
    from opentimestamps.core.notary import PendingAttestation

    detached = _load(path)
    if _bitcoin_attestations(detached):
        return True
    changed = False
    for sub in list(_directly_attested(detached.timestamp)):
        for att in list(sub.attestations):
            if not isinstance(att, PendingAttestation) or att.uri not in _UPGRADE_CALENDARS:
                continue
            try:
                sub.merge(RemoteCalendar(att.uri).get_timestamp(sub.msg, timeout=_TIMEOUT_S))
                changed = True
            except Exception as exc:  # noqa: BLE001 — not yet mined, or calendar down
                log.debug("upgrade via %s: %s", att.uri, exc)
    if changed:
        _save(detached, path)
    return bool(_bitcoin_attestations(detached))


def _pending_proofs(root: Path) -> Iterator[Path]:
    witness_dir = root / "witness"
    if witness_dir.is_dir():
        yield from sorted(witness_dir.glob("*/*.txt.ots"))


def run_witness(root: Path, night: str, *, push: bool = True) -> dict[str, object]:
    """Nightly step: commit, stamp the night's HEAD, complete older proofs.

    Fails (visibly, via the nightly status) when no calendar accepts the
    stamp or when backups have stalled — a stalled backup leaves nothing
    new to witness.
    """
    try:
        import opentimestamps  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "opentimestamps missing: pip install 'smriti[witness]'"}
    if not (root / ".git").exists():
        return {"ok": False, "error": f"no git repo at {root}"}

    result: dict[str, object] = {"lock_cleared": clear_stale_lock(root)}
    _commit(root, "nightly")

    head_time = int(_git(root, "log", "-1", "--format=%ct").stdout.strip() or 0)
    dirty = bool(_git(root, "status", "--porcelain").stdout.strip())
    stalled = dirty and time.time() - head_time > _STALE_HEAD_S
    if stalled:
        when = datetime.fromtimestamp(head_time, timezone.utc).date().isoformat()
        result["error"] = f"backups stalled: last commit {when}, tree has changes"

    anchored = pending = 0
    for proof in _pending_proofs(root):
        if upgrade(proof):
            anchored += 1
        else:
            pending += 1

    manifest = _manifest_path(root, night)
    calendars = -1  # already stamped this night
    if not manifest.exists() or not manifest.with_name(manifest.name + ".ots").exists():
        calendars = stamp(write_manifest(root, night))
        if calendars < _MIN_CALENDARS:
            result["error"] = f"only {calendars} calendar(s) accepted the stamp"
        _commit(root, "witness", push=push)

    unpushed = _git(root, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
    result.update({
        "ok": "error" not in result,
        "head": _git(root, "rev-parse", "--short", "HEAD").stdout.strip(),
        "calendars": calendars,
        "anchored": anchored,
        "pending": pending,
        "unpushed": int(unpushed) if unpushed.isdigit() else None,
    })
    return result


# ── verification ─────────────────────────────────────────────────────


@dataclass
class NightCheck:
    """What one night's witness still proves."""

    night: str
    head: str
    digest_ok: bool
    in_history: bool
    block_height: int | None = None
    block_time: int | None = None
    anchor_ok: bool | None = None  # None: not checked (offline / pending)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _block(height: int) -> tuple[str, int] | None:
    """(merkle root, block time) from the first explorer that answers."""
    for base in _EXPLORERS:
        try:
            with urllib.request.urlopen(f"{base}/block-height/{height}",
                                        timeout=_TIMEOUT_S) as resp:
                block_hash = resp.read().decode().strip()
            with urllib.request.urlopen(f"{base}/block/{block_hash}",
                                        timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            return str(data["merkle_root"]), int(data["timestamp"])
        except Exception as exc:  # noqa: BLE001
            log.warning("explorer %s: %s", base, exc)
    return None


def check_proof(data: bytes, proof: Path, *, offline: bool = False) -> NightCheck:
    """Check a proof against its file's bytes (and, online, against Bitcoin)."""
    detached = _load(proof)
    check = NightCheck(night="", head="", in_history=True,
                       digest_ok=bytes(detached.file_digest)
                       == hashlib.sha256(_normalized(data)).digest())
    if not check.digest_ok:
        check.problems.append("file no longer matches its timestamp")
    attestations = _bitcoin_attestations(detached)
    if not attestations:
        return check
    height, msg = min(attestations)
    check.block_height = height
    if offline:
        return check
    block = _block(height)
    if block is None:
        check.problems.append(f"no explorer reachable to check block {height}")
        return check
    merkle_root, check.block_time = block
    # Proofs carry the merkle root in internal byte order; explorers show it reversed.
    check.anchor_ok = msg[::-1].hex() == merkle_root
    if not check.anchor_ok:
        check.problems.append(f"proof does not match Bitcoin block {height}")
    return check


def verify(root: Path, *, offline: bool = False) -> list[NightCheck]:
    """Check every night's manifest: proof valid, anchored, still in history."""
    checks: list[NightCheck] = []
    witness_dir = root / "witness"
    manifests = sorted(witness_dir.glob("*/*.txt")) if witness_dir.is_dir() else []
    for manifest in manifests:
        fields = read_manifest(manifest)
        proof = manifest.with_name(manifest.name + ".ots")
        if proof.exists():
            check = check_proof(manifest.read_bytes(), proof, offline=offline)
        else:
            check = NightCheck(night="", head="", digest_ok=False, in_history=True,
                               problems=["timestamp proof missing"])
        check.night = fields.get("night", manifest.stem)
        check.head = fields.get("head", "")
        check.in_history = bool(check.head) and _git(
            root, "merge-base", "--is-ancestor", check.head, "HEAD").returncode == 0
        if not check.in_history:
            check.problems.append("commit no longer in history — rewritten after this night")
        checks.append(check)
    return checks
