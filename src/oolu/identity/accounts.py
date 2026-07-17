"""Local user accounts — multi-user web hosting without an external IdP.

The gateway's identity model does not change: bearer tokens validated
against a configured provider, authority resolved from **stored** grants
(a token's claimed roles are never consulted). What a self-hoster lacks is
the identity provider itself; this module is that provider, scoped
honestly:

- passwords are **scrypt**-hashed (stdlib): per-user random salt, cost
  parameters recorded next to the hash so they can be raised later
  without invalidating old records;
- login mints a short-lived HS256 token from the install's own secret.
  Symmetric signing is the self-host trade (one install, one secret) —
  and ``assert_production_identity`` still refuses HMAC providers for
  production-money deployments, exactly as designed;
- **roles become grants**: creating a user writes ``AuthorityGrant`` rows
  into the ``IdentityStore``, so a forged token claim still buys nothing;
- login failures are uniform ("invalid credentials" whether the user is
  unknown, wrong-passworded, or disabled — no account enumeration), cost
  the same scrypt work either way, and repeated failures lock the
  username briefly (the login route is public; scrypt alone is not a
  rate limit);
- disabling a user stops future logins; outstanding tokens age out with
  their short TTL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from ..persistence import Migration, migrate
from .errors import AuthenticationError
from .models import AuthorityGrant, Role, Tenant
from .store import IdentityStore
from .tokens import Hs256Signer

ADMIN_ROLE = "admin"
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")
MIN_PASSWORD_LENGTH = 8

# Interactive-login scrypt cost (~tens of ms): slow enough to blunt offline
# cracking of a stolen store, fast enough that login stays snappy.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

LOCKOUT_THRESHOLD = 10  # consecutive failures ...
LOCKOUT_SECONDS = 60.0  # ... buy this much enforced patience


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``scrypt$N$r$p$salt$hash`` — self-describing, upgradeable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"passwords must be at least {MIN_PASSWORD_LENGTH} characters")
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${digest_b64}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time comparison against a stored hash; malformed = False."""
    try:
        scheme, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A real hash to verify unknown-user attempts against, so "no such user"
# costs the same scrypt work as "wrong password" — uniform timing.
_DECOY_HASH = hash_password(secrets.token_urlsafe(16))


class UserAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    username: str
    tenant_id: str
    password_hash: str
    roles: tuple[str, ...] = ()
    disabled: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str
    expires_at: datetime
    tenant_id: str
    principal: str


def _create_users(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS local_users (
               username TEXT PRIMARY KEY,
               tenant_id TEXT NOT NULL,
               password_hash TEXT NOT NULL,
               roles TEXT NOT NULL,
               disabled INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL
           )"""
    )


def _drop_users(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS local_users")


USER_STORE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(up=_create_users, down=_drop_users),
)


class LocalUserStore:
    """SQLite-backed user records (thread-safe)."""

    def __init__(self, path: str | Path = ":memory:"):
        location = (
            str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        )
        if location != ":memory:":
            Path(location).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(location, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._db, USER_STORE_MIGRATIONS, label="local-users")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add(self, user: UserAccount) -> None:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO local_users VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        user.username,
                        user.tenant_id,
                        user.password_hash,
                        json.dumps(list(user.roles)),
                        1 if user.disabled else 0,
                        user.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"user already exists: {user.username}") from exc
            self._db.commit()

    def get(self, username: str) -> UserAccount | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM local_users WHERE username = ?", (username,)
            ).fetchone()
        if row is None:
            return None
        return UserAccount(
            username=row["username"],
            tenant_id=row["tenant_id"],
            password_hash=row["password_hash"],
            roles=tuple(json.loads(row["roles"])),
            disabled=bool(row["disabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list(self, tenant_id: str) -> list[UserAccount]:
        with self._lock:
            rows = self._db.execute(
                "SELECT username FROM local_users WHERE tenant_id = ? "
                "ORDER BY username",
                (tenant_id,),
            ).fetchall()
        return [self.get(row["username"]) for row in rows]

    def set_disabled(self, username: str, disabled: bool) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "UPDATE local_users SET disabled = ? WHERE username = ?",
                (1 if disabled else 0, username),
            )
            self._db.commit()
        return cursor.rowcount > 0

    def set_password_hash(self, username: str, password_hash: str) -> bool:
        with self._lock:
            cursor = self._db.execute(
                "UPDATE local_users SET password_hash = ? WHERE username = ?",
                (password_hash, username),
            )
            self._db.commit()
        return cursor.rowcount > 0


class LocalAccountService:
    """Users in, short-lived gateway tokens out; roles become stored grants."""

    def __init__(
        self,
        users: LocalUserStore,
        identity: IdentityStore,
        signer: Hs256Signer,
        *,
        token_ttl_seconds: int = 8 * 3600,
        clock: Callable[[], datetime] | None = None,
    ):
        self._users = users
        self._identity = identity
        self._signer = signer
        self._ttl = token_ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._failures: dict[str, tuple[int, datetime | None]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def bootstrap(self, *, tenant: str, username: str, password: str) -> bool:
        """Ensure the tenant, the admin role, and one admin user exist.

        Idempotent: re-running against an existing install changes nothing
        (in particular it never resets the admin's password). Returns True
        only when the admin user was actually created.
        """
        self._identity.add_tenant(Tenant(tenant_id=tenant, name=tenant))
        self._identity.add_role(
            Role(tenant_id=tenant, name=ADMIN_ROLE, permissions=frozenset({"*"}))
        )
        if self._users.get(username) is not None:
            return False
        self.create_user(
            username,
            password,
            tenant=tenant,
            roles=(ADMIN_ROLE,),
            granted_by="bootstrap",
        )
        return True

    def create_user(
        self,
        username: str,
        password: str,
        *,
        tenant: str,
        roles: tuple[str, ...] | list[str] = (),
        granted_by: str = "system",
    ) -> UserAccount:
        if not _USERNAME_RE.match(username):
            raise ValueError(
                "usernames are 3-64 characters of letters, digits, '.', '_', '-'"
            )
        user = UserAccount(
            username=username,
            tenant_id=tenant,
            password_hash=hash_password(password),
            roles=tuple(roles),
        )
        self._users.add(user)
        # Authority comes from STORED grants, never token claims — so the
        # roles are written where the resolver actually looks.
        for role in user.roles:
            self._identity.add_grant(
                AuthorityGrant(
                    tenant_id=tenant,
                    principal_id=username,
                    role_name=role,
                    granted_by=granted_by,
                )
            )
        return user

    # ------------------------------------------------------------------ #
    def login(
        self, username: str, password: str, *, now: datetime | None = None
    ) -> LoginResult:
        moment = now or self._clock()
        self._check_lockout(username, moment)
        user = self._users.get(username)
        # The decoy keeps unknown-user attempts as slow as wrong-password
        # ones; the single failure message keeps them indistinguishable.
        ok = verify_password(password, user.password_hash if user else _DECOY_HASH)
        if user is None or user.disabled or not ok:
            self._record_failure(username, moment)
            raise AuthenticationError("invalid credentials")
        with self._lock:
            self._failures.pop(username, None)
        expires_at = moment + timedelta(seconds=self._ttl)
        token = self._signer.mint(
            subject=username,
            tenant_id=user.tenant_id,
            ttl_seconds=self._ttl,
            now=moment,
            amr=["pwd"],
        )
        return LoginResult(
            token=token,
            expires_at=expires_at,
            tenant_id=user.tenant_id,
            principal=username,
        )

    def external_login(
        self, username: str, *, method: str = "sso", now: datetime | None = None
    ) -> LoginResult:
        """A session for an identity verified OUTSIDE the password store —
        an IdP-verified sign-in (e.g. a validated Google id_token). The
        caller vouches for the verification; this only refuses accounts
        that don't exist or are disabled, and stamps how the user arrived
        (``amr``) so the token records it wasn't a password."""
        moment = now or self._clock()
        user = self._users.get(username)
        if user is None or user.disabled:
            raise AuthenticationError("invalid credentials")
        expires_at = moment + timedelta(seconds=self._ttl)
        token = self._signer.mint(
            subject=username,
            tenant_id=user.tenant_id,
            ttl_seconds=self._ttl,
            now=moment,
            amr=[method],
        )
        return LoginResult(
            token=token,
            expires_at=expires_at,
            tenant_id=user.tenant_id,
            principal=username,
        )

    def set_disabled(self, username: str, disabled: bool) -> bool:
        return self._users.set_disabled(username, disabled)

    def change_password(self, username: str, new_password: str) -> bool:
        return self._users.set_password_hash(username, hash_password(new_password))

    def users(self, tenant: str) -> list[UserAccount]:
        return self._users.list(tenant)

    def user(self, username: str) -> UserAccount | None:
        return self._users.get(username)

    # ------------------------------------------------------------------ #
    def _check_lockout(self, username: str, moment: datetime) -> None:
        with self._lock:
            count, locked_until = self._failures.get(username, (0, None))
        if locked_until is not None and moment < locked_until:
            raise AuthenticationError("too many failed attempts; try again shortly")

    def _record_failure(self, username: str, moment: datetime) -> None:
        with self._lock:
            count, _ = self._failures.get(username, (0, None))
            count += 1
            locked_until = None
            if count >= LOCKOUT_THRESHOLD:
                locked_until = moment + timedelta(seconds=LOCKOUT_SECONDS)
                count = 0  # a lockout served is a slate cleaned
            self._failures[username] = (count, locked_until)


# --------------------------------------------------------------------------- #
# Forgot-password's staged key.                                                #
# --------------------------------------------------------------------------- #
PENDING_PASSWORD_TTL_MINUTES = 30


class PendingPasswordStore:
    """The e-mailed new password waits HERE — it never replaces the real one.

    The one-step forgot-password flow used to set the account's password
    the moment anyone asked, which handed strangers a lockout lever:
    knowing an address was enough to force-reset its account. Staging
    closes that: the mailed password is a SECOND key with a short life
    (:data:`PENDING_PASSWORD_TTL_MINUTES`), the current password keeps
    working untouched, and the staged one becomes real only when its
    owner actually signs in with it — which is also the moment inbox
    control is proven. A sign-in with the CURRENT password clears any
    staged key, so an attacker's request dies the moment the real owner
    shows up. Hashes only, same scrypt scheme as the account store.
    """

    _SCHEMA = """CREATE TABLE IF NOT EXISTS pending_passwords (
        username TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )"""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(self._SCHEMA)

    def stage(
        self, username: str, password: str, *, now: datetime | None = None
    ) -> None:
        """Park a fresh password beside the real one (replacing any prior
        staged key), alive for the TTL."""
        moment = now or self._clock()
        expires = moment + timedelta(minutes=PENDING_PASSWORD_TTL_MINUTES)
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO pending_passwords
                     (username, password_hash, expires_at, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(username) DO UPDATE SET
                     password_hash = excluded.password_hash,
                     expires_at = excluded.expires_at,
                     created_at = excluded.created_at""",
                (
                    username,
                    hash_password(password),
                    expires.isoformat(),
                    moment.isoformat(),
                ),
            )

    def take(
        self, username: str, password: str, *, now: datetime | None = None
    ) -> bool:
        """True iff ``password`` is the staged, unexpired key — and spend
        it: a staged password promotes exactly once. Expired rows die on
        the way through; a mismatch leaves the row (the real owner's mail
        may still be in flight while a stranger guesses)."""
        moment = now or self._clock()
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT password_hash, expires_at FROM pending_passwords"
                " WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return False
        if moment > datetime.fromisoformat(row["expires_at"]):
            self.clear(username)
            return False
        if not verify_password(password, row["password_hash"]):
            return False
        self.clear(username)
        return True

    def clear(self, username: str) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "DELETE FROM pending_passwords WHERE username = ?", (username,)
            )
