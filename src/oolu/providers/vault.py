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

    def resolve(self, ref: "CredentialRef | str") -> str:
        """Return the secret. The boundary: callers must use it
        transiently only. Accepts a bare ref id too — the skills
        layer's ``SecretProvider`` protocol speaks strings."""
        ref_id = ref.ref_id if isinstance(ref, CredentialRef) else str(ref)
        with self._lock:
            stored = self._store.get(ref_id)
            if stored is None:
                raise KeyError(f"unknown credential: {ref_id}")
            if stored.revoked:
                raise RevokedCredential(f"credential {ref_id} is revoked")
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

    def revoke(self, ref: "CredentialRef | str") -> None:
        ref_id = ref.ref_id if isinstance(ref, CredentialRef) else str(ref)
        with self._lock:
            stored = self._store.get(ref_id)
            if stored is not None:
                stored.revoked = True

    def is_revoked(self, ref: "CredentialRef | str") -> bool:
        ref_id = ref.ref_id if isinstance(ref, CredentialRef) else str(ref)
        with self._lock:
            stored = self._store.get(ref_id)
            return stored is None or stored.revoked

    def redact(self, text: str) -> str:
        """Replace any stored secret occurring in ``text`` with a placeholder."""
        with self._lock:
            secrets = [s.secret for s in self._store.values() if s.secret]
        for secret in secrets:
            if secret:
                text = text.replace(secret, "<REDACTED>")
        return text


_VAULT_SCHEMA = """CREATE TABLE IF NOT EXISTS secret_vault (
    ref_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    sealed TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)"""


class DurableSecretVault:
    """The vault surface over the durable connection, sealed at rest (V2).

    Same boundary contract as :class:`SecretVault` — refs out, headers
    minted at call time, redaction over every error path — but the rows
    survive the process: a node's API key provided once authenticates
    every later run. Sealing is the keyring's stdlib encrypt-then-MAC
    under the install's ``machine.key`` (the one durable key file model
    keys and TOTP seeds already trust), so the plaintext appears nowhere
    a grep of the database or a backup of the rows could find it."""

    def __init__(self, conn, *, key_path) -> None:
        from pathlib import Path

        from .keyring import _load_machine_key, _open, _seal

        self._conn = conn
        self._machine_key = _load_machine_key(Path(key_path))
        self._seal = lambda plaintext: _seal(self._machine_key, plaintext)
        self._open = lambda sealed: _open(self._machine_key, sealed)
        with self._conn.transaction() as db:
            db.execute(_VAULT_SCHEMA)

    def put(
        self, secret: str, *, kind: str = "secret", metadata: dict | None = None
    ) -> CredentialRef:
        import json
        from datetime import UTC, datetime

        if not secret:
            raise ValueError("a credential needs a value")
        ref_id = uuid4().hex
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO secret_vault
                     (ref_id, kind, sealed, metadata_json, revoked, created_at)
                   VALUES (?, ?, ?, ?, 0, ?)""",
                (
                    ref_id,
                    kind,
                    self._seal(secret.encode("utf-8")),
                    json.dumps(dict(metadata or {})),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return CredentialRef(ref_id=ref_id, kind=kind)

    def resolve(self, ref: "CredentialRef | str") -> str:
        """Return the secret — transiently only, exactly like the
        in-memory vault. Accepts a bare ref id too (the skills layer's
        ``SecretProvider`` protocol speaks strings)."""
        ref_id = ref.ref_id if isinstance(ref, CredentialRef) else str(ref)
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT sealed, revoked FROM secret_vault WHERE ref_id = ?",
                (ref_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown credential: {ref_id}")
        if row["revoked"]:
            raise RevokedCredential(f"credential {ref_id} is revoked")
        return self._open(row["sealed"]).decode("utf-8")

    def authorize_header(
        self,
        ref: "CredentialRef | str",
        *,
        scheme: str = "Bearer",
        header: str = "Authorization",
    ) -> dict[str, str]:
        """Mint an auth header. The only sanctioned way a secret leaves the vault."""
        secret = self.resolve(ref)
        value = f"{scheme} {secret}" if scheme else secret
        return {header: value}

    def revoke(self, ref: "CredentialRef | str") -> None:
        ref_id = ref.ref_id if isinstance(ref, CredentialRef) else str(ref)
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE secret_vault SET revoked = 1 WHERE ref_id = ?",
                (ref_id,),
            )

    def is_revoked(self, ref: "CredentialRef | str") -> bool:
        ref_id = ref.ref_id if isinstance(ref, CredentialRef) else str(ref)
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT revoked FROM secret_vault WHERE ref_id = ?", (ref_id,)
            ).fetchone()
        return row is None or bool(row["revoked"])

    def redact(self, text: str) -> str:
        """Replace any stored, unrevoked secret in ``text`` — the count
        of node credentials is human-sized, so decrypting them for the
        scrub is bounded work."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT sealed FROM secret_vault WHERE revoked = 0"
            ).fetchall()
        for row in rows:
            try:
                secret = self._open(row["sealed"]).decode("utf-8")
            except Exception:  # noqa: BLE001 - a corrupt row must not stop redaction
                continue
            if secret:
                text = text.replace(secret, "<REDACTED>")
        return text
