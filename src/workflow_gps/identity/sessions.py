"""Session lifecycle: issue from a verified assertion, authenticate, revoke.

A session is the only way identity enters the rest of the system. It is derived
from a validated OIDC assertion (never from caller-supplied text), is stored with
an expiry, and can be revoked. Authentication re-checks expiry and revocation on
every use, so a stolen or stale session cannot be replayed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .errors import AuthenticationError, SessionExpiredError, SessionRevokedError
from .models import Claims, Session
from .store import IdentityStore
from .tokens import OidcValidator

# Authentication methods that raise the assurance level (step-up eligible).
_STRONG_AMR = frozenset({"mfa", "otp", "hwk", "webauthn", "pop"})
_STRONG_ACR = frozenset({"high", "aal2", "aal3", "mfa"})


def default_assurance(claims: Claims) -> int:
    """Assurance 2 when the assertion shows strong authentication, else 1."""
    if _STRONG_ACR.intersection({claims.acr} if claims.acr else set()):
        return 2
    if _STRONG_AMR.intersection(claims.amr):
        return 2
    return 1


class SessionManager:
    def __init__(
        self,
        store: IdentityStore,
        validator: OidcValidator,
        *,
        default_ttl_seconds: int = 3600,
        assurance_fn: Callable[[Claims], int] = default_assurance,
    ):
        self._store = store
        self._validator = validator
        self._ttl = default_ttl_seconds
        self._assurance_fn = assurance_fn

    def login(
        self,
        token: str,
        *,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> Session:
        moment = now or datetime.now(UTC)
        claims = self._validator.validate(token, now=moment)
        session = Session(
            principal_id=claims.subject,
            principal_kind=claims.principal_kind,
            tenant_id=claims.tenant_id,
            issued_at=moment,
            expires_at=moment + timedelta(seconds=ttl_seconds or self._ttl),
            assurance_level=self._assurance_fn(claims),
            amr=list(claims.amr),
            source_issuer=claims.issuer,
        )
        self._store.save_session(session)
        return session

    def authenticate(self, session_id: str, *, now: datetime | None = None) -> Session:
        moment = now or datetime.now(UTC)
        session = self._store.get_session(session_id)
        if session is None:
            raise AuthenticationError("unknown session")
        if session.revoked:
            raise SessionRevokedError("session has been revoked")
        if moment > session.expires_at:
            raise SessionExpiredError("session has expired")
        return session

    def revoke(self, session_id: str) -> bool:
        return self._store.revoke_session(session_id)
