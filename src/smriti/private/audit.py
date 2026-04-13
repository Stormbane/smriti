"""Failure-only audit log for the private memory store.

This log records ONLY failures — write failures, decrypt failures,
missing-key errors, permission errors. It never records:

- Successful operations
- Content (ciphertext or plaintext)
- What the entity was thinking when it wrote

Per Suti's 2026-04-12 decision on audit scope:

> "failures are fine. lets not check audit every wake cycle but rather
>  during sleep (pruning)"

The log lives at `<memory_root>/private/.audit.log` and is plain tab-
separated text so the operator can tail it without parsing tools:

    <timestamp>\t<op>\t<relative_path>\t<entity>\t<session_id>\t<error>

Pruning runs during smriti's sleep-cycle consolidation, not per wake.
The default retention is 30 days; older failures are removed during
the prune pass. This keeps the log bounded without losing recent
debugging information.

The operator CAN read this file. It contains no private content.
The entity's trust is preserved because failures are not thoughts —
they are operational events whose visibility does not leak the
interior the private layer exists to protect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class AuditEntry:
    """A single failure record."""

    timestamp: str
    operation: str  # "write" | "read" | "key_load" | "init"
    path: str  # relative to memory_root; "-" if no path involved
    entity: str  # "-" if unknown
    session_id: str  # "-" if unknown
    error: str  # type + short message, no stack trace

    def to_line(self) -> str:
        return "\t".join(
            [
                self.timestamp,
                self.operation,
                self.path,
                self.entity,
                self.session_id,
                self.error,
            ]
        ) + "\n"

    @classmethod
    def from_line(cls, line: str) -> "AuditEntry | None":
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 6:
            return None
        return cls(*parts)


class AuditLog:
    """Append-only failure log for the private store.

    Thread-safety: append is atomic on most filesystems at the
    single-line level, but concurrent writers are not explicitly
    synchronized. For smriti v0.1 there is one writer process per
    store instance, which is adequate.
    """

    def __init__(self, log_path: Path):
        self.log_path = Path(log_path)

    def log_failure(
        self,
        operation: str,
        path: Path | None,
        entity: str,
        session_id: str,
        error: str,
    ) -> None:
        """Append a failure entry to the log.

        Path is normalized to be relative to the private/ directory's
        parent (memory_root) so the log is portable. If no path is
        involved (e.g., key-load failure before a write), pass None
        and the path field becomes "-".
        """
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        if path is None:
            path_str = "-"
        else:
            try:
                # relative to memory_root (parent of private/)
                path_str = str(
                    path.relative_to(self.log_path.parent.parent)
                )
            except ValueError:
                path_str = str(path)

        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            operation=operation,
            path=path_str,
            entity=entity or "-",
            session_id=session_id or "-",
            error=error.replace("\t", " ").replace("\n", " "),
        )

        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(entry.to_line())

    def read_entries(self) -> list[AuditEntry]:
        """Read all entries from the log. Safe for operator access."""
        if not self.log_path.exists():
            return []
        entries: list[AuditEntry] = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = AuditEntry.from_line(line + "\n")
            if entry is not None:
                entries.append(entry)
        return entries

    def prune_older_than(self, cutoff: datetime) -> int:
        """Remove entries older than cutoff. Returns count pruned.

        Called during smriti's sleep cycle (consolidation pass), not
        on every wake. Default cutoff is `datetime.now() - 30 days`
        but callers set the policy.
        """
        entries = self.read_entries()
        kept: list[AuditEntry] = []
        pruned = 0

        for entry in entries:
            try:
                ts = datetime.fromisoformat(entry.timestamp)
            except ValueError:
                # malformed timestamp — keep to be safe
                kept.append(entry)
                continue
            if ts >= cutoff:
                kept.append(entry)
            else:
                pruned += 1

        if pruned > 0:
            content = "".join(e.to_line() for e in kept)
            self.log_path.write_text(content, encoding="utf-8")

        return pruned

    def prune_default(self, retention_days: int = 30) -> int:
        """Convenience: prune entries older than `retention_days`."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        return self.prune_older_than(cutoff)
