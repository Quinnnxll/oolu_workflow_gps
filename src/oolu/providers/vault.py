"""The credential vault — the single boundary credentials are allowed to cross.

Adapters never hold a raw secret; they hold a :class:`CredentialRef` and ask the
vault to mint an authorization header at call time. The secret therefore exists
only transiently inside the vault call and in the outbound request to the provider
— never in an adapter's fields, its audit log, a result, or an exception. ``redact``
scrubs any registered secret out of text before it could be logged.

This in-memory vault is the local boundary; a KMS/secret-manager-backed vault is
the production adapter implementing the same surface.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from uuid import uuid4

from .errors import RevokedCredential


@dataclass(frozen=True)
class CredentialRef:
    """An opaque handle to a stored secret. Its repr never reveals the secret."""

    ref_id: str
    kind: str = "secret"

    def __repr__(self) -> str:  # defensive: never let a ref print a secret
        return f"CredentialRef(ref_id={self.ref_id!r}, kind={self.kind!r})"


@dataclass
class _StoredCredential:
    secret: str
    kind: str
    metadata: dict = field(default_factory=dict)
    revoked: bool = False


class SecretVault:
    def __init__(self) -> None:
        self._store: dict[str, _StoredCredential] = {}
        self._lock = threading.RLock()

    def put(
        self, secret: str, *, kind: str = "secret", metadata: dict | None = None
    ) -> CredentialRef:
        ref_id = uuid4().hex
        with self._lock:
            self._store[ref_id] = _StoredCredential(
                secret=secret, kind=kind, metadata=dict(metadata or {})
            )
        return CredentialRef(ref_id=ref_id, kind=kind)

    def resolve(self, ref: CredentialRef) -> str:
        """Return the secret. The boundary: callers must use it transiently only."""
        with self._lock:
            stored = self._store.get(ref.ref_id)
            if stored is None:
                raise KeyError(f"unknown credential: {ref.ref_id}")
            if stored.revoked:
                raise RevokedCredential(f"credential {ref.ref_id} is revoked")
            return stored.secret

    def authorize_header(
        self,
        ref: CredentialRef,
        *,
        scheme: str = "Bearer",
        header: str = "Authorization",
    ) -> dict[str, str]:
        """Mint an auth header. The only sanctioned way a secret leaves the vault."""
        secret = self.resolve(ref)
        value = f"{scheme} {secret}" if scheme else secret
        return {header: value}

    def revoke(self, ref: CredentialRef) -> None:
        with self._lock:
            stored = self._store.get(ref.ref_id)
            if stored is not None:
                stored.revoked = True

    def is_revoked(self, ref: CredentialRef) -> bool:
        with self._lock:
            stored = self._store.get(ref.ref_id)
            return stored is None or stored.revoked

    def redact(self, text: str) -> str:
        """Replace any stored secret occurring in ``text`` with a placeholder."""
        with self._lock:
            secrets = [s.secret for s in self._store.values() if s.secret]
        for secret in secrets:
            if secret:
                text = text.replace(secret, "<REDACTED>")
        return text
