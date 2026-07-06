"""API keys: how other systems call OoLu's task execution.

When the node/path database is big enough to be worth calling, callers are
programs, not people — they need long-lived machine credentials, scoped to
the little they should touch:

- A key's secret (``oolu_sk_...``) is shown ONCE at creation and stored
  only as a SHA-256 hash — a database leak leaks no credentials.
- Every key carries **scopes**; the gateway refuses any route outside them
  (an execution key can submit and read runs, and nothing else — not
  settings, not files, not payments, not other keys).
- Keys are revocable and tenant-scoped, and record when they last worked.
"""

from __future__ import annotations

import hashlib
import secrets as secrets_module
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

KEY_PREFIX = "oolu_sk_"

# Everything a machine caller may be granted. Execution + discovery; the
# human surfaces (settings, files, payments, key management) are absent by
# construction, not by configuration.
KNOWN_SCOPES: frozenset[str] = frozenset({"runs:submit", "runs:read", "market:read"})
DEFAULT_SCOPES: tuple[str, ...] = ("runs:submit", "runs:read", "market:read")


class ApiKeyError(ValueError):
    pass


class ApiKeyRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    key_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    tenant_id: str
    principal_id: str  # the human the key acts on behalf of
    name: str
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


_SCHEMA = """CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,
    key_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class ApiKeyService:
    """Issue, authenticate, list, and revoke keys over the durable conn."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def issue(
        self,
        *,
        tenant: str,
        principal: str,
        name: str,
        scopes: list[str] | None = None,
    ) -> tuple[ApiKeyRecord, str]:
        """Mint a key. The returned secret exists only in this return value."""
        wanted = tuple(scopes) if scopes else DEFAULT_SCOPES
        unknown = sorted(set(wanted) - KNOWN_SCOPES)
        if unknown:
            allowed = ", ".join(sorted(KNOWN_SCOPES))
            raise ApiKeyError(
                f"unknown scopes {unknown}; grantable scopes are: {allowed}"
            )
        if not name.strip():
            raise ApiKeyError("a key needs a name")
        secret = KEY_PREFIX + secrets_module.token_urlsafe(32)
        record = ApiKeyRecord(
            tenant_id=tenant,
            principal_id=principal,
            name=name.strip(),
            scopes=wanted,
        )
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO api_keys (key_hash, key_id, tenant_id, payload_json)"
                " VALUES (?, ?, ?, ?)",
                (_hash(secret), record.key_id, tenant, record.model_dump_json()),
            )
        return record, secret

    def authenticate(self, secret: str) -> ApiKeyRecord | None:
        """The secret's hash is the lookup key; a revoked key is no key."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM api_keys WHERE key_hash = ?",
                (_hash(secret),),
            ).fetchone()
        if row is None:
            return None
        record = ApiKeyRecord.model_validate_json(row["payload_json"])
        if not record.active:
            return None
        used = record.model_copy(update={"last_used_at": datetime.now(UTC)})
        self._update(_hash(secret), used)
        return used

    def list(self, *, tenant: str) -> list[ApiKeyRecord]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM api_keys WHERE tenant_id = ?"
                " ORDER BY rowid ASC",
                (tenant,),
            ).fetchall()
        return [ApiKeyRecord.model_validate_json(r["payload_json"]) for r in rows]

    def revoke(self, key_id: str, *, tenant: str) -> bool:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT key_hash, payload_json FROM api_keys"
                " WHERE key_id = ? AND tenant_id = ?",
                (key_id, tenant),
            ).fetchone()
        if row is None:
            return False
        record = ApiKeyRecord.model_validate_json(row["payload_json"])
        if not record.active:
            return False
        self._update(
            row["key_hash"],
            record.model_copy(update={"revoked_at": datetime.now(UTC)}),
        )
        return True

    def _update(self, key_hash: str, record: ApiKeyRecord) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "UPDATE api_keys SET payload_json = ? WHERE key_hash = ?",
                (record.model_dump_json(), key_hash),
            )


def scope_allows(scopes: frozenset[str], method: str, path: str) -> bool:
    """The whole machine-callable surface, spelled out.

    Anything not listed here — settings, files, payments, chat, work,
    nodeplace, auth, key management — is refused for API keys regardless
    of which scopes they hold: absent by construction.
    """
    if path.startswith("/v1/runs"):
        if method == "GET":
            return "runs:read" in scopes
        return "runs:submit" in scopes
    if path.startswith("/v1/listings") and method == "GET":
        return "market:read" in scopes
    if path.startswith("/v1/market"):
        # candidates/quotes/assemble are read-only previews (no money moves).
        return "market:read" in scopes
    return False
