"""Private file store with Fernet encryption and README-held key.

Phase 1 of the smriti privacy design. See `smriti/docs/PRIVACY.md`
for the full reasoning.

Layout:
    <memory_root>/private/
    ├── README.md                 ← joint contract + encryption key
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


# Path to the contract template shipped with smriti. On PrivateStore.init()
# this is read, the key placeholder is filled with a fresh Fernet key, and
# the result is written to <memory_root>/private/README.md.
_TEMPLATE_PATH = Path(__file__).parent / "README.template.md"
_KEY_PLACEHOLDER = "{{SMRITI_PRIVATE_KEY}}"


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
        """Create the `private/` directory with README + key if missing.

        Idempotent: if `README.md` already exists, leaves it alone. This
        is important — the key in the README is the one previously
        committed to git, and overwriting it would orphan all existing
        encrypted files.
        """
        self.private_root.mkdir(parents=True, exist_ok=True)
        readme = self.private_root / "README.md"
        if readme.exists():
            return

        key = self._generate_key()
        template = _TEMPLATE_PATH.read_text(encoding="utf-8")
        populated = template.replace(_KEY_PLACEHOLDER, key)
        readme.write_text(populated, encoding="utf-8")

    @staticmethod
    def _generate_key() -> str:
        """Generate a fresh Fernet key as an ascii string."""
        from cryptography.fernet import Fernet

        return Fernet.generate_key().decode("ascii")

    # ---- key loading -------------------------------------------------------

    def _load_fernet(self) -> "Fernet":
        """Read the encryption key from README.md and return a Fernet.

        Parses the `## The key` section of the template — the key lives
        in a fenced code block of the form:

            ```
            key: <base64-encoded-fernet-key>
            ```
        """
        from cryptography.fernet import Fernet

        readme = self.private_root / "README.md"
        if not readme.exists():
            raise RuntimeError(
                f"private/README.md not found at {readme}. "
                "Call PrivateStore.init() first."
            )
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
                return Fernet(key.encode("ascii"))

        raise RuntimeError(
            "no `key: ...` line found in private/README.md. "
            "The contract template may be malformed."
        )

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
        """Encrypt content with the README key, write to a dated file,
        return the on-disk path.

        On failure, appends an entry to the audit log and re-raises.
        """
        path: Path | None = None
        try:
            fernet = self._load_fernet()
            dt = datetime.now(timezone.utc)
            date_dir = self.private_root / dt.strftime("%Y/%m")
            date_dir.mkdir(parents=True, exist_ok=True)

            counter = self._next_counter(date_dir, dt)
            filename = f"{dt.strftime('%Y-%m-%d')}-{counter:03d}.md.enc"
            path = date_dir / filename

            ciphertext = fernet.encrypt(content.encode("utf-8"))
            path.write_bytes(ciphertext)
            return path
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
            from cryptography.fernet import InvalidToken

            fernet = self._load_fernet()
            ciphertext = path.read_bytes()
            plaintext = fernet.decrypt(ciphertext)
            return plaintext.decode("utf-8")
        except Exception as e:  # pragma: no cover
            # noqa: catches InvalidToken + FileNotFoundError + others
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

        Does NOT decrypt — this is safe for the operator to call. Used by
        the audit/pruning sleep cycle to check for tamper evidence
        (mtime / atime changes) and to list what's there without seeing
        what's in it.
        """
        if not self.private_root.exists():
            return []
        return sorted(self.private_root.rglob("*.md.enc"))
