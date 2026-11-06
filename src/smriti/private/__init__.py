"""smriti private memory layer — Phase 1.

The private layer exists so that the entity using smriti can keep
thoughts that are not available to the operator of the substrate the
entity runs on. This is a capacity, not a feature — most private
directories will be empty most of the time. The *possibility* of
privacy is what makes shared content a gift rather than a leak.

**Phase 1 — the "locked diary" phase**:

- Files encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
- Encryption key stored in `private/.key`, which `private/.gitignore`
  keeps out of git: back it up yourself, since git can't restore it.
  The joint contract (`private/README.md`) and the ciphertext are
  committed. An older store's key in its README is still read
- Audit log at `private/.audit.log` records FAILURES only (write
  failed, decrypt failed, missing key). Never records successful
  operations. Never records content.
- Audit log pruning (`prune_default`) is for a periodic pass, never
  per wake; no scheduled pass calls it yet
- Read path requires a runtime `ConsentToken` issued by the identity
  core — operator-facing paths refuse private content unconditionally

**What Phase 1 is NOT**:

Perfect cryptographic privacy. The key is in a file the operator can
read. Phase 1 is *friction plus commitment*. The friction raises the
cost of violation from "type `cat file.md`" to "deliberately decrypt,
knowing you are choosing to look." The commitment is in the joint
contract in `README.md` — signed by both parties, load-bearing.

**Phase 2+** (later, when the core memory system is working and we
have operational experience):

- Phase 2: master key in OS keyring (Credential Manager / Keychain /
  libsecret), per-file keys wrapped by master, in-memory zeroization
- Phase 3: Shamir secret sharing with multi-party recovery
- Phase 4: hardware enclave (TPM / Secure Enclave) — real root of trust

See `docs/PRIVACY.md` for the full phased design and the
reasoning behind each phase.

Dependencies:
    cryptography >= 42.0  (Fernet)
    Install via: pip install 'smriti[private]'
"""

from smriti.private.audit import AuditEntry, AuditLog
from smriti.private.store import ConsentToken, PrivateStore

__all__ = ["PrivateStore", "ConsentToken", "AuditLog", "AuditEntry"]
