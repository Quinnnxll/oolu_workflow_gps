"""Sign in with Google — the RFC 8252 native-app flow over the local gateway.

The shape: the app asks the gateway to *begin* a flow and opens the returned
Google consent URL in the system browser (PKCE, no client secret needed to be
confidential); Google redirects the browser to this same loopback gateway's
callback route; the gateway exchanges the code, validates the **id_token**
against the pinned issuer/audience/nonce with a pluggable
:class:`~oolu.identity.tokens.SignatureVerifier` (Google's JWKS in production,
HMAC in offline tests); and the app *polls finish* to collect a normal local
session token. The browser never carries the session token — only the one-shot
``state`` connects the two channels, and the result is handed out exactly once.

Identity linking, not replacement: a verified Google subject maps to a local
:class:`~oolu.identity.accounts.UserAccount` through the ``identity_links``
table. A fresh Google sign-in creates an account; an existing link signs into
the account it points at; and an *authenticated* begin (``link_to``) attaches
Google to the caller's current account — the local-mode upgrade path that
keeps every learned path and file exactly where it was.
"""

from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable
from urllib.parse import urlencode

from ..providers.errors import ProviderError, classify_status
from ..providers.google import pkce_challenge
from .accounts import LocalAccountService, LoginResult
from .errors import AuthenticationError
from .tokens import OidcValidator, SignatureVerifier

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class SignInError(ValueError):
    """A refused sign-in flow: bad state, bad token, conflicting link."""


@dataclass(frozen=True)
class GoogleSignInConfig:
    """The per-install Google OAuth client. The client_id is not a secret;
    a Desktop-app client's client_secret is issued by Google but explicitly
    not confidential — both may live in plain config/env."""

    client_id: str
    client_secret: str = ""
    auth_endpoint: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: str = "https://oauth2.googleapis.com/token"
    issuers: tuple[str, ...] = GOOGLE_ISSUERS
    scopes: tuple[str, ...] = ("openid", "email", "profile")


@dataclass
class _Flow:
    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    created_at: datetime
    link_to: tuple[str, str] | None = None  # (tenant, username) when linking
    result: LoginResult | None = None
    error: str | None = None


_LINKS_SCHEMA = """CREATE TABLE IF NOT EXISTS identity_links (
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    username TEXT NOT NULL,
    email TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (provider, subject)
)"""


class IdentityLinkStore:
    """provider+subject -> local account, over the durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_LINKS_SCHEMA)

    def link(
        self, *, provider: str, subject: str, tenant: str, username: str,
        email: str, at: datetime,
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO identity_links
                     (provider, subject, tenant_id, username, email, linked_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider, subject) DO UPDATE SET
                     email = excluded.email""",
                (provider, subject, tenant, username, email, at.isoformat()),
            )

    def lookup(self, provider: str, subject: str) -> dict | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT tenant_id, username, email FROM identity_links"
                " WHERE provider = ? AND subject = ?",
                (provider, subject),
            ).fetchone()
        if row is None:
            return None
        return {
            "tenant": row["tenant_id"],
            "username": row["username"],
            "email": row["email"],
        }

    def links_for(self, username: str) -> list[dict]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT provider, subject, email, linked_at FROM identity_links"
                " WHERE username = ? ORDER BY provider",
                (username,),
            ).fetchall()
        return [
            {
                "provider": r["provider"],
                "email": r["email"],
                "linked_at": r["linked_at"],
            }
            for r in rows
        ]


_USERNAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def username_from_email(email: str) -> str:
    base = _USERNAME_SAFE.sub("-", email.split("@", 1)[0]).strip("-._") or "user"
    return base if len(base) >= 3 else f"{base}-user"


class GoogleSignIn:
    """The flow broker: begin -> (browser) callback -> finish."""

    def __init__(
        self,
        accounts: LocalAccountService,
        links: IdentityLinkStore,
        config: GoogleSignInConfig,
        *,
        verifier: SignatureVerifier,
        transport,  # providers.HttpTransport
        default_tenant: str = "main",
        flow_ttl_seconds: int = 600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._accounts = accounts
        self._links = links
        self._config = config
        self._verifier = verifier
        self._transport = transport
        self._tenant = default_tenant
        self._ttl = timedelta(seconds=flow_ttl_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._flows: dict[str, _Flow] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def begin(
        self, redirect_uri: str, *, link_to: tuple[str, str] | None = None
    ) -> dict:
        """Start a flow: mint state + PKCE + nonce, return the consent URL."""
        self._sweep()
        flow = _Flow(
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(16),
            code_verifier=secrets.token_urlsafe(48),
            redirect_uri=redirect_uri,
            created_at=self._clock(),
            link_to=link_to,
        )
        with self._lock:
            self._flows[flow.state] = flow
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._config.scopes),
            "state": flow.state,
            "nonce": flow.nonce,
            "code_challenge": pkce_challenge(flow.code_verifier),
            "code_challenge_method": "S256",
        }
        return {
            "auth_url": f"{self._config.auth_endpoint}?{urlencode(params)}",
            "state": flow.state,
        }

    def callback(self, params: dict[str, str]) -> str:
        """The browser's leg: exchange the code, verify, sign in or link.

        Returns the signed-in username for the "return to the app" page.
        Failures are remembered on the flow so ``finish`` can report them
        to the app, then raised for the browser page."""
        state = params.get("state", "")
        with self._lock:
            flow = self._flows.get(state)
        if flow is None or self._expired(flow):
            raise SignInError("this sign-in link has expired — start again")
        try:
            if "error" in params:
                raise SignInError(f"Google refused: {params['error']}")
            code = params.get("code")
            if not code:
                raise SignInError("the authorization code is missing")
            claims = self._verified_claims(self._exchange(flow, code), flow)
            flow.result = self._sign_in(claims, flow)
            return flow.result.principal
        except (SignInError, AuthenticationError, ProviderError) as exc:
            flow.error = str(exc)
            raise SignInError(str(exc)) from exc

    def finish(self, state: str) -> dict:
        """The app's leg: poll until the browser leg lands. Single-use."""
        with self._lock:
            flow = self._flows.get(state)
        if flow is None or self._expired(flow):
            raise SignInError("unknown or expired sign-in")
        if flow.error is not None:
            with self._lock:
                self._flows.pop(state, None)
            raise SignInError(flow.error)
        if flow.result is None:
            return {"status": "pending"}
        with self._lock:
            self._flows.pop(state, None)
        result = flow.result
        return {
            "status": "complete",
            "token": result.token,
            "expires_at": result.expires_at.isoformat(),
            "tenant": result.tenant_id,
            "principal": result.principal,
        }

    # ------------------------------------------------------------------ #
    def _exchange(self, flow: _Flow, code: str) -> str:
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._config.client_id,
            "redirect_uri": flow.redirect_uri,
            "code_verifier": flow.code_verifier,
        }
        if self._config.client_secret:
            body["client_secret"] = self._config.client_secret
        response = self._transport.request(
            "POST",
            self._config.token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
        error_cls = classify_status(response.status)
        if error_cls is not None:
            raise SignInError(f"token exchange failed (status {response.status})")
        id_token = response.json.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise SignInError("Google's answer carried no id_token")
        return id_token

    def _verified_claims(self, id_token: str, flow: _Flow) -> dict:
        """Verify signature + registered claims + nonce; return the payload.

        Google id_tokens carry no tenant claim, so this validates the OIDC
        registered claims directly with the same primitives OidcValidator
        uses — algorithm pinned to the verifier, ``none`` refused."""
        header, payload, signing_input, signature = OidcValidator._split(id_token)
        alg = header.get("alg")
        if not alg or alg.lower() == "none":
            raise SignInError("token algorithm 'none' is not permitted")
        if alg != self._verifier.algorithm:
            raise SignInError(f"unexpected token algorithm {alg!r}")
        if not self._verifier.verify(signing_input, signature, header):
            raise SignInError("id_token signature verification failed")

        if payload.get("iss") not in self._config.issuers:
            raise SignInError("id_token issuer is not Google")
        raw_aud = payload.get("aud", [])
        audiences = [raw_aud] if isinstance(raw_aud, str) else list(raw_aud)
        if self._config.client_id not in audiences:
            raise SignInError("id_token audience is not this app")
        exp = payload.get("exp")
        if exp is None or self._clock() > datetime.fromtimestamp(
            exp, UTC
        ) + timedelta(seconds=60):
            raise SignInError("id_token has expired")
        if payload.get("nonce") != flow.nonce:
            raise SignInError("id_token nonce does not match this flow")
        if not isinstance(payload.get("sub"), str) or not payload["sub"]:
            raise SignInError("id_token carries no subject")
        email = payload.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise SignInError("id_token carries no email")
        if payload.get("email_verified") is False:
            raise SignInError("this Google account's email is not verified")
        return payload

    def _sign_in(self, claims: dict, flow: _Flow) -> LoginResult:
        subject, email = claims["sub"], claims["email"]
        now = self._clock()
        existing = self._links.lookup("google", subject)

        if flow.link_to is not None:
            tenant, username = flow.link_to
            if existing is not None and existing["username"] != username:
                raise SignInError(
                    "this Google account is already linked to a different user"
                )
            self._links.link(
                provider="google", subject=subject, tenant=tenant,
                username=username, email=email, at=now,
            )
            return self._accounts.external_login(username, now=now)

        if existing is not None:
            return self._accounts.external_login(existing["username"], now=now)

        # First arrival: a fresh account named after the email, linked.
        username = self._fresh_username(email)
        self._accounts.create_user(
            username,
            secrets.token_urlsafe(24),  # never told to anyone; Google IS the key
            tenant=self._tenant,
            granted_by="google-signin",
        )
        self._links.link(
            provider="google", subject=subject, tenant=self._tenant,
            username=username, email=email, at=now,
        )
        return self._accounts.external_login(username, now=now)

    def _fresh_username(self, email: str) -> str:
        base = username_from_email(email)
        candidate = base
        for suffix in range(2, 100):
            if self._accounts.user(candidate) is None:
                return candidate
            candidate = f"{base}-{suffix}"
        raise SignInError("could not derive a free username")

    def _expired(self, flow: _Flow) -> bool:
        return self._clock() - flow.created_at > self._ttl

    def _sweep(self) -> None:
        with self._lock:
            dead = [s for s, f in self._flows.items() if self._expired(f)]
            for state in dead:
                self._flows.pop(state, None)
