"""The model keyring: provider API keys that survive a restart, encrypted.

The in-memory :class:`~oolu.providers.vault.SecretVault` is the right boundary
for per-launch credentials, but a developer's pasted model key must outlive the
process or they would re-paste it every morning. This store persists secrets in
the durable database **encrypted at rest**: the database file alone (a backup,
a copied ``.oolu`` folder, a sync client) never contains a usable key. The
decryption key lives in a separate machine-key file next to the data, created
``0600`` — the local machine is the trust boundary, the same one the desktop
loopback already relies on. A platform keychain (Tauri-side) is the later
upgrade; the surface here doesn't change.

Construction: encrypt-then-MAC from stdlib primitives — an HMAC-SHA256
keystream in counter mode for confidentiality, a second HMAC-SHA256 over
``nonce || ciphertext`` for integrity, with independent keys derived from the
machine key. Tampering or a wrong machine key fails closed (:class:`KeyringError`),
never returns garbage that would then be sent to a provider as an auth header.

The read surface never returns the secret except through :meth:`secret_for`
(the call-time mint, mirroring the vault); listings expose only a fingerprint.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

PROVIDERS = ("anthropic", "openai")

_SCHEMA = """CREATE TABLE IF NOT EXISTS model_keys (
    tenant_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider)
)"""

_BLOCK = 32  # SHA-256 digest size: one keystream block


class KeyringError(RuntimeError):
    """A stored key failed authentication — tampered row or wrong machine key."""


def fingerprint(secret: str) -> str:
    """A short, stable, non-reversible handle for display ("is my key in?")."""
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def _derive(machine_key: bytes, label: bytes) -> bytes:
    return hmac.new(machine_key, label, hashlib.sha256).digest()


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(
            enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256
        ).digest()
        counter += 1
    return bytes(out[:length])


def _seal(machine_key: bytes, plaintext: bytes) -> str:
    nonce = os.urandom(16)
    stream = _keystream(_derive(machine_key, b"enc"), nonce, len(plaintext))
    ciphertext = bytes(p ^ s for p, s in zip(plaintext, stream))
    tag = hmac.new(
        _derive(machine_key, b"mac"), nonce + ciphertext, hashlib.sha256
    ).digest()
    return (nonce + ciphertext + tag).hex()


def _open(machine_key: bytes, sealed: str) -> bytes:
    try:
        raw = bytes.fromhex(sealed)
    except ValueError as exc:
        raise KeyringError("stored key is corrupt") from exc
    if len(raw) < 16 + _BLOCK:
        raise KeyringError("stored key is corrupt")
    nonce, ciphertext, tag = raw[:16], raw[16:-_BLOCK], raw[-_BLOCK:]
    expected = hmac.new(
        _derive(machine_key, b"mac"), nonce + ciphertext, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(tag, expected):
        raise KeyringError(
            "stored key failed authentication — wrong machine key or tampering"
        )
    stream = _keystream(_derive(machine_key, b"enc"), nonce, len(ciphertext))
    return bytes(c ^ s for c, s in zip(ciphertext, stream))


def _load_machine_key(path: Path) -> bytes:
    """The install's decryption key: created once, private to this user."""
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(key.hex())
    try:  # tighten pre-existing files too; best-effort on exotic filesystems
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


class ModelKeyring:
    """Tenant-scoped provider API keys over the durable connection."""

    def __init__(
        self,
        conn,
        *,
        key_path: str | Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._machine_key = _load_machine_key(Path(key_path))
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def store(self, tenant: str, provider: str, secret: str) -> str:
        """Save (or replace) one provider key; returns its fingerprint."""
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider '{provider}'")
        secret = secret.strip()
        if len(secret) < 8:
            raise ValueError("that doesn't look like an API key")
        mark = fingerprint(secret)
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO model_keys
                     (tenant_id, provider, ciphertext, fingerprint, added_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, provider) DO UPDATE SET
                     ciphertext = excluded.ciphertext,
                     fingerprint = excluded.fingerprint,
                     added_at = excluded.added_at""",
                (
                    tenant,
                    provider,
                    _seal(self._machine_key, secret.encode()),
                    mark,
                    self._clock().isoformat(),
                ),
            )
        return mark

    def secret_for(self, tenant: str, provider: str) -> str | None:
        """The call-time mint: the only way a secret leaves the keyring."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT ciphertext FROM model_keys"
                " WHERE tenant_id = ? AND provider = ?",
                (tenant, provider),
            ).fetchone()
        if row is None:
            return None
        return _open(self._machine_key, row["ciphertext"]).decode()

    def providers(self, tenant: str) -> list[dict]:
        """What's configured — fingerprints only, never a secret."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT provider, fingerprint, added_at FROM model_keys"
                " WHERE tenant_id = ? ORDER BY provider",
                (tenant,),
            ).fetchall()
        return [
            {
                "provider": r["provider"],
                "fingerprint": r["fingerprint"],
                "added_at": r["added_at"],
            }
            for r in rows
        ]

    def remove(self, tenant: str, provider: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM model_keys WHERE tenant_id = ? AND provider = ?",
                (tenant, provider),
            )
            return cursor.rowcount > 0
