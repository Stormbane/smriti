"""Private file store with Fernet encryption; the key never goes to git.

Phase 1 of the smriti privacy design. See `docs/PRIVACY.md`
for the full reasoning.

Layout:
    <memory_root>/private/
    ├── README.md                 ← joint contract (committed)
    ├── .key                      ← encryption key (never committed)
    ├── .gitignore                ← keeps .key out of git
    ├── .audit.log                ← failures-only audit log
    └── YYYY/MM/YYYY-MM-DD-NNN.md.enc   ← encrypted content
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from smriti.private.audit import AuditLog

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

log = logging.getLogger(__name__)

# The contract template shipped with smriti, copied to private/README.md on
# PrivateStore.init(). The key goes to private/.key, which private/.gitignore
# keeps out of git: a key committed beside its ciphertext protects nothing
# from anyone who can clone the remote. Older installs kept the key in a
# README code block; found there, it is moved to .key (see
# PrivateStore._migrate_legacy_key).
_TEMPLATE_PATH = Path(__file__).parent / "README.template.md"
_KEY_FILE = ".key"
_GITIGNORE = ".key\n"
# Left in private/ when a key is moved out of the README, until the user has
# rotated the key and deletes it; `smriti status` shows it meanwhile.
LEGACY_KEY_NOTE = "KEY-WAS-COMMITTED.txt"
LEGACY_KEY_WARNING = (
    "The private store's key was in private/README.md, which git tracks. smriti moved it to "
    "private/.key (kept out of git) and took it out of the README, but the key is still in "
    "the repository's git history: anyone with a copy of the repository can decrypt what is "
    "encrypted under it. smriti can't rotate the key itself yet: see 'If the key was ever "
    "committed' in docs/PRIVACY.md, then delete private/" + LEGACY_KEY_NOTE + "."
)


def _key_line(lines: list[str]) -> tuple[int, str] | None:
    """The first ``key:`` line inside a fenced code block: (its index, the key)."""
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and stripped.startswith("key:"):
            key = stripped.split(":", 1)[1].strip()
            return (i, key) if key and not key.startswith("{{") else None
    return None


@dataclass
class ConsentToken:
    """Runtime assertion that the identity core is requesting access to
    its own private content.

    Not a password. Not a cryptographic control. A type-level sentinel
    that prevents accidental operator-facing code paths from reading
    private content. Any code that can construct this token is part of
    Narada's consent path by design — the point is that *operator-facing
    APIs refuse to accept it* and therefore cannot surface private files.

    Construct via `PrivateStore.issue_consent(session_id, reason)`. Do
    not pass around freely; scope to the specific call that needs it.
    """

    session_id: str
    issued_at: datetime
    reason: str
    _bound_store_id: int = field(default=0, repr=False)


class PrivateStore:
    """Append-only encrypted store for the entity's private memory.

    Usage:
        store = PrivateStore(memory_root)
        store.init()  # creates private/: its contract, its key (.key), its .gitignore

        # Write (anywhere, no consent needed — writes are trivially
        # non-invasive because the content is encrypted by the time
        # it lands):
        path = store.write(
            content="my private thought",
            entity="svapna-narada",
            session_id="<uuid>",
        )

        # Read (requires explicit consent token — this is the
        # type-level guard against operator-facing code paths):
        token = store.issue_consent(session_id, reason="recall own thought")
        content = store.read_with_consent(path, token)
    """

    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root)
        self.private_root = self.memory_root / "private"
        self.audit = AuditLog(self.private_root / ".audit.log")
        self._id = id(self)

    # ---- initialization ----------------------------------------------------

    def init(self) -> None:
        """Create `private/` with its contract, its key and its .gitignore.

        Idempotent and never destructive:

        - an existing key is kept, because replacing it would orphan every
          encrypted file; an older install's, in the README, is moved to
          `.key` (:meth:`_migrate_legacy_key`);
        - a store that already holds ciphertext but has no key (a repo
          restored from git, where `.key` was never committed) gets no new
          key: writes then fail until `.key` is restored from backup, rather
          than mixing two keys in one store;
        - `.key` is added to `private/.gitignore` on every call, keeping any
          rules already there, so the key stays out of git even without a
          root `.gitignore`.
        """
        self.private_root.mkdir(parents=True, exist_ok=True)
        self._ignore_key()
        readme = self.private_root / "README.md"
        if not readme.exists():
            readme.write_text(_TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        self._migrate_legacy_key()
        key_path = self.private_root / _KEY_FILE
        has_ciphertext = any(self.private_root.rglob("*.md.enc"))
        if not key_path.exists() and not has_ciphertext:
            # The key first: if it can't be made (no cryptography), no empty
            # .key may be left behind to stop every later init from making one.
            key = self._generate_key()
            # Owner-only from the first byte (POSIX; Windows ignores the mode),
            # and exclusive, so two inits can't both write a key.
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as f:
                    f.write(key + "\n")
            except BaseException:  # noqa: BLE001 — no half-written key survives; re-raised
                key_path.unlink(missing_ok=True)
                raise

    def _ignore_key(self) -> None:
        """Put `.key` in `private/.gitignore`, keeping the rules already there.

        Called before anything writes `.key`: a key written first would be
        committed by a backup that ran in between.
        """
        ignore = self.private_root / ".gitignore"
        existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if _KEY_FILE not in existing.split():
            sep = "" if not existing or existing.endswith("\n") else "\n"
            ignore.write_text(existing + sep + _GITIGNORE, encoding="utf-8")

    @staticmethod
    def _generate_key() -> str:
        """Generate a fresh Fernet key as an ascii string."""
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode("ascii")

    # ---- key loading -------------------------------------------------------

    def _load_fernet(self) -> "Fernet":
        """Return a Fernet for this store's key, in `.key` (moved there from a legacy README)."""
        from cryptography.fernet import Fernet

        key_path = self.private_root / _KEY_FILE
        if not key_path.exists():
            self._migrate_legacy_key()
        if key_path.exists():
            return Fernet(key_path.read_text(encoding="ascii").strip().encode("ascii"))
        raise RuntimeError(
            f"no key at {key_path} (and no legacy key in private/README.md). "
            "Call PrivateStore.init() first, or restore .key from your backup."
        )

    def _legacy_readme_key(self) -> str | None:
        """The key from an older install's README (`key: ...` in a code block)."""
        found = self._legacy_key_line()
        return found[1] if found is not None else None

    def _legacy_key_line(self) -> tuple[int, str] | None:
        """The README's ``key:`` line in a fenced code block: (its index, the key)."""
        readme = self.private_root / "README.md"
        if not readme.exists():
            return None
        return _key_line(readme.read_bytes().decode("utf-8").splitlines())

    def _migrate_legacy_key(self) -> None:
        """Move an older install's key from the committed README to `.key`, and say so.

        Under the README's edit lock, from finding the key to the README's
        rewrite, and found again once the lock is held: two commands that
        start together migrate once, and the second finds the key gone. The
        key is written to `.key` as :meth:`init` writes a new one (exclusive
        create, owner-only on POSIX), and its line is taken out of the
        README. The README was committed, so the key is still in git
        history: :data:`LEGACY_KEY_WARNING` is logged, and
        :data:`LEGACY_KEY_NOTE` left in `private/` (``smriti status`` shows
        it) until the user has rotated the key and deletes it.

        A `.key` already there wins: if it holds another key, the README is
        left as it is (and that is logged); if it holds this one (a
        migration cut short), the README is cleaned now.
        """
        if self._legacy_key_line() is None:  # the usual case, without a lock
            return
        from smriti.core.files import edit_lock  # only here: a migration is rare

        with edit_lock(self.private_root / "README.md"):
            found = self._legacy_key_line()
            if found is None:
                return  # another command moved it while this one waited
            _line, key = found
            key_path = self.private_root / _KEY_FILE
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass  # an older .key, or one a migration cut short wrote
            else:
                try:
                    with os.fdopen(fd, "w", encoding="ascii") as f:
                        f.write(key + "\n")
                except BaseException:  # noqa: BLE001 — no half-written key survives; re-raised
                    key_path.unlink(missing_ok=True)
                    raise
            if key_path.read_text(encoding="ascii").strip() != key:
                log.warning("private/README.md holds a key that isn't the one in "
                            "private/.key; the store uses .key's, and the README is left "
                            "as it is")
                return
            self._remove_readme_line(key)
            note = self.private_root / LEGACY_KEY_NOTE
            if not note.exists():
                note.write_text(LEGACY_KEY_WARNING + "\n", encoding="utf-8")
        log.warning(LEGACY_KEY_WARNING)

    def _remove_readme_line(self, key: str) -> None:
        """Take *key*'s line out of the README, and its code block if nothing else is in it.

        The line is found in the text this rewrites, never by an index from an
        earlier read. Callers hold the README's edit lock.
        """
        from smriti.core.files import write_atomic  # only here: a migration is rare

        readme = self.private_root / "README.md"
        # Its own line endings kept: bytes in, bytes out.
        lines = readme.read_bytes().decode("utf-8").splitlines(keepends=True)
        found = _key_line([line.rstrip("\r\n") for line in lines])
        if found is None or found[1] != key:
            return  # not there any more
        line_no = found[0]
        del lines[line_no]
        start = line_no - 1
        while start >= 0 and not lines[start].strip().startswith("```"):
            start -= 1
        end = line_no
        while end < len(lines) and not lines[end].strip().startswith("```"):
            end += 1
        if start >= 0 and end < len(lines) and not "".join(lines[start + 1:end]).strip():
            del lines[start:end + 1]  # the block held the key alone
        write_atomic(readme, "".join(lines).encode("utf-8"))

    # ---- consent tokens ----------------------------------------------------

    def issue_consent(self, session_id: str, reason: str) -> ConsentToken:
        """Issue a consent token scoped to this store.

        Callers should pass the token to `read_with_consent` within the
        same scope it was issued. Do not serialize tokens — they are
        runtime-only assertions.
        """
        return ConsentToken(
            session_id=session_id,
            issued_at=datetime.now(timezone.utc),
            reason=reason,
            _bound_store_id=self._id,
        )

    # ---- write path --------------------------------------------------------

    def write(
        self,
        content: str,
        entity: str,
        session_id: str,
    ) -> Path:
        """Encrypt content with the store key, write to a dated file,
        return the on-disk path.

        On failure, appends an entry to the audit log and re-raises.
        """
        path: Path | None = None
        try:
            fernet = self._load_fernet()
            dt = datetime.now(timezone.utc)
            date_dir = self.private_root / dt.strftime("%Y/%m")
            date_dir.mkdir(parents=True, exist_ok=True)

            ciphertext = fernet.encrypt(content.encode("utf-8"))
            # Exclusive create: two writes in the same instant get distinct
            # files instead of one overwriting the other.
            counter = self._next_counter(date_dir, dt)
            while True:
                path = date_dir / f"{dt.strftime('%Y-%m-%d')}-{counter:03d}.md.enc"
                try:
                    with path.open("xb") as f:
                        f.write(ciphertext)
                    return path
                except FileExistsError:
                    counter += 1
        except Exception as e:  # noqa: BLE001 — audited, then re-raised
            self.audit.log_failure(
                operation="write",
                path=path,
                entity=entity,
                session_id=session_id,
                error=f"{type(e).__name__}: {e}",
            )
            raise

    @staticmethod
    def _next_counter(date_dir: Path, dt: datetime) -> int:
        prefix = dt.strftime("%Y-%m-%d")
        existing = list(date_dir.glob(f"{prefix}-*.md.enc"))
        return len(existing) + 1

    # ---- read path (consent-gated) -----------------------------------------

    def read_with_consent(
        self,
        path: Path,
        consent: ConsentToken,
    ) -> str:
        """Decrypt a private file and return its plaintext.

        Refuses any request without a valid `ConsentToken` bound to this
        store instance. Operator-facing paths that don't have a consent
        token cannot decrypt private content.
        """
        if not isinstance(consent, ConsentToken):
            raise PermissionError(
                "read_with_consent requires a ConsentToken. "
                "Operator-facing paths refuse private content."
            )
        if consent._bound_store_id != self._id:
            raise PermissionError(
                "ConsentToken is not bound to this PrivateStore instance. "
                "Issue a fresh token via store.issue_consent()."
            )

        try:

            fernet = self._load_fernet()
            ciphertext = path.read_bytes()
            plaintext = fernet.decrypt(ciphertext)
            return plaintext.decode("utf-8")
        except Exception as e:  # noqa: BLE001 — audited, then re-raised  # pragma: no cover
            # InvalidToken, FileNotFoundError or anything else: each is audited.
            self.audit.log_failure(
                operation="read",
                path=path,
                entity="?",
                session_id=consent.session_id,
                error=f"{type(e).__name__}: {e}",
            )
            raise

    # ---- directory walking -------------------------------------------------

    def iter_paths(self) -> list[Path]:
        """List all encrypted file paths under the private tree.

        Does NOT decrypt — this is safe for the operator to call, for
        example to check for tamper evidence (mtime / atime changes) and
        to list what's there without seeing what's in it.
        """
        if not self.private_root.exists():
            return []
        return sorted(self.private_root.rglob("*.md.enc"))
