"""Private file store with Fernet encryption; the key never goes to git.

Phase 1 of the smriti privacy design. See `smriti/docs/PRIVACY.md`
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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from smriti.private.audit import AuditLog

if TYPE_CHECKING:
    from cryptography.fernet import Fernet


# The contract template shipped with smriti, copied to private/README.md on
# PrivateStore.init(). The key goes to private/.key, which private/.gitignore
# keeps out of git: a key committed beside its ciphertext protects nothing
# from anyone who can clone the remote. Older installs kept the key in a
# README code block; it is still read from there when .key is absent.
_TEMPLATE_PATH = Path(__file__).parent / "README.template.md"
_KEY_FILE = ".key"
_GITIGNORE = ".key\n"


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
        store.init()  # creates private/ + README with generated key

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

        - an existing key (in `.key`, or in an older install's README) is
          kept, because replacing it would orphan every encrypted file;
        - a store that already holds ciphertext but has no key (a repo
          restored from git, where `.key` was never committed) gets no new
          key: writes then fail until `.key` is restored from backup, rather
          than mixing two keys in one store;
        - `.key` is added to `private/.gitignore` on every call, keeping any
          rules already there, so the key stays out of git even without a
          root `.gitignore`.
        """
        self.private_root.mkdir(parents=True, exist_ok=True)
        ignore = self.private_root / ".gitignore"
        existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if _KEY_FILE not in existing.split():
            sep = "" if not existing or existing.endswith("\n") else "\n"
            ignore.write_text(existing + sep + _GITIGNORE, encoding="utf-8")
        readme = self.private_root / "README.md"
        if not readme.exists():
            readme.write_text(_TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        key_path = self.private_root / _KEY_FILE
        has_ciphertext = any(self.private_root.rglob("*.md.enc"))
        if (not key_path.exists() and self._legacy_readme_key() is None
                and not has_ciphertext):
            key_path.write_text(self._generate_key() + "\n", encoding="ascii")

    @staticmethod
    def _generate_key() -> str:
        """Generate a fresh Fernet key as an ascii string."""
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode("ascii")

    # ---- key loading -------------------------------------------------------

    def _load_fernet(self) -> "Fernet":
        """Return a Fernet for this store's key: `.key`, else a legacy README key."""
        from cryptography.fernet import Fernet

        key_path = self.private_root / _KEY_FILE
        if key_path.exists():
            return Fernet(key_path.read_text(encoding="ascii").strip().encode("ascii"))
        legacy = self._legacy_readme_key()
        if legacy is not None:
            return Fernet(legacy.encode("ascii"))
        raise RuntimeError(
            f"no key at {key_path} (and no legacy key in private/README.md). "
            "Call PrivateStore.init() first, or restore .key from your backup."
        )

    def _legacy_readme_key(self) -> str | None:
        """The key from an older install's README (`key: ...` in a code block)."""
        readme = self.private_root / "README.md"
        if not readme.exists():
            return None
        text = readme.read_text(encoding="utf-8")

        # Find a line starting with "key:" inside any fenced code block.
        in_fence = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence and stripped.startswith("key:"):
                key = stripped.split(":", 1)[1].strip()
                return key if key and not key.startswith("{{") else None
        return None

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
        except Exception as e:
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
        except Exception as e:  # pragma: no cover
            # Catches InvalidToken, FileNotFoundError and anything else.
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
