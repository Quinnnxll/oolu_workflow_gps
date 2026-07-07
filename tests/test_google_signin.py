"""Sign in with Google, end to end against a fake Google.

The fake is honest about the protocol: the consent URL's PKCE/state/nonce
are real, the token exchange is a scripted transport, and the id_token is a
real JWT minted with the test verifier's key — so every check the service
performs (signature, issuer, audience, expiry, nonce, verified email) runs
against real material. The trust chain under test:

    begin -> browser callback (code -> id_token) -> finish -> session token
    that authenticates against the same gateway.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from test_chat_model_router import FakeTransport
from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp
from oolu.identity import (
    Hs256Signer,
    Hs256Verifier,
    IdentityStore,
    LocalAccountService,
    LocalUserStore,
)
from oolu.identity.google_signin import (
    GoogleSignIn,
    GoogleSignInConfig,
    IdentityLinkStore,
    SignInError,
)

CLIENT_ID = "12345-test.apps.googleusercontent.com"
GOOGLE_SECRET = "google-signing-secret-for-tests"


def _google_id_token(
    *, sub="g-sub-1", email="quinn@mphepo.io", nonce, audience=CLIENT_ID,
    issuer="https://accounts.google.com", verified=True, ttl=600,
):
    """A structurally real Google id_token, HS256-signed with the test key."""
    signer = Hs256Signer(secret=GOOGLE_SECRET, issuer=issuer, audience=audience)
    return signer.mint(
        subject=sub,
        tenant_id="unused-by-google",  # Google sends no tenant; ours is extra
        ttl_seconds=ttl,
        extra={"email": email, "email_verified": verified, "nonce": nonce},
    )


class Rig:
    """Accounts + links + service over a scripted Google."""

    def __init__(self, tmp_path):
        self.users = LocalUserStore(":memory:")
        self.identity = IdentityStore(tmp_path / "identity.db")
        self.signer = Hs256Signer(
            secret="local-gateway-signing-secret!!", issuer="local", audience="oolu"
        )
        self.accounts = LocalAccountService(self.users, self.identity, self.signer)
        self.conn = DurableConnection(tmp_path / "links.db")
        self.links = IdentityLinkStore(self.conn)
        self.transport = FakeTransport()
        self.google = GoogleSignIn(
            self.accounts,
            self.links,
            GoogleSignInConfig(client_id=CLIENT_ID),
            verifier=Hs256Verifier(GOOGLE_SECRET),
            transport=self.transport,
            default_tenant="t1",
        )

    def close(self):
        self.users.close()
        self.conn.close()

    # One browser round-trip: begin -> scripted exchange -> callback.
    def sign_in(self, *, sub="g-sub-1", email="quinn@mphepo.io", link_to=None):
        begun = self.google.begin(
            "http://127.0.0.1:8765/v1/auth/google/callback", link_to=link_to
        )
        params = parse_qs(urlparse(begun["auth_url"]).query)
        self.transport.script(
            "oauth2.googleapis.com",
            200,
            {"id_token": _google_id_token(sub=sub, email=email, nonce=params["nonce"][0])},
        )
        self.google.callback({"state": begun["state"], "code": "auth-code-1"})
        return self.google.finish(begun["state"])


@pytest.fixture()
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_the_consent_url_is_a_real_pkce_request(rig):
    begun = rig.google.begin("http://127.0.0.1:8765/v1/auth/google/callback")
    url = urlparse(begun["auth_url"])
    params = parse_qs(url.query)
    assert url.hostname == "accounts.google.com"
    assert params["client_id"] == [CLIENT_ID]
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["openid email profile"]
    assert params["state"] == [begun["state"]]
    assert params["nonce"][0]  # replay protection rides the id_token


def test_first_google_arrival_creates_a_linked_account(rig):
    from oolu.identity import Tenant

    rig.identity.add_tenant(Tenant(tenant_id="t1", name="t1"))
    done = rig.sign_in()

    assert done["status"] == "complete"
    assert done["principal"] == "quinn"
    assert done["tenant"] == "t1"
    # The exchange carried PKCE, never a password.
    exchange = rig.transport.requests[-1]["body"]
    assert exchange["code_verifier"]
    assert "password" not in exchange
    # The account exists and the identity is linked.
    assert rig.accounts.user("quinn") is not None
    assert rig.links.lookup("google", "g-sub-1")["username"] == "quinn"


def test_returning_google_user_signs_into_the_same_account(rig):
    from oolu.identity import Tenant

    rig.identity.add_tenant(Tenant(tenant_id="t1", name="t1"))
    first = rig.sign_in()
    again = rig.sign_in()  # a fresh flow, same Google subject
    assert first["principal"] == again["principal"] == "quinn"
    assert len(rig.accounts.users("t1")) == 1  # no duplicate account


def test_username_collisions_get_suffixes_not_hijacks(rig):
    from oolu.identity import Tenant

    rig.identity.add_tenant(Tenant(tenant_id="t1", name="t1"))
    rig.accounts.create_user("quinn", "quinns-own-password", tenant="t1")

    done = rig.sign_in()  # same email localpart, different person
    assert done["principal"] == "quinn-2"
    # The password account is untouched.
    assert rig.accounts.login("quinn", "quinns-own-password").principal == "quinn"


def test_linking_attaches_google_to_the_existing_account(rig):
    from oolu.identity import Tenant

    rig.identity.add_tenant(Tenant(tenant_id="t1", name="t1"))
    rig.accounts.create_user("local", "local-password", tenant="t1")

    done = rig.sign_in(link_to=("t1", "local"))
    assert done["principal"] == "local"  # same account, nothing migrated

    # And from now on a plain Google sign-in lands in that account.
    again = rig.sign_in()
    assert again["principal"] == "local"


def test_a_google_identity_cannot_be_linked_twice(rig):
    from oolu.identity import Tenant

    rig.identity.add_tenant(Tenant(tenant_id="t1", name="t1"))
    rig.accounts.create_user("alice", "alices-password", tenant="t1")
    rig.accounts.create_user("bob", "bobs-password", tenant="t1")
    rig.sign_in(link_to=("t1", "alice"))

    with pytest.raises(SignInError, match="already linked"):
        rig.sign_in(link_to=("t1", "bob"))


def test_forged_and_stale_tokens_are_refused(rig):
    from oolu.identity import Tenant

    rig.identity.add_tenant(Tenant(tenant_id="t1", name="t1"))

    def attempt(**token_kwargs):
        begun = rig.google.begin("http://127.0.0.1:8765/v1/auth/google/callback")
        params = parse_qs(urlparse(begun["auth_url"]).query)
        kwargs = {"nonce": params["nonce"][0], **token_kwargs}
        rig.transport.script(
            "oauth2.googleapis.com", 200, {"id_token": _google_id_token(**kwargs)}
        )
        with pytest.raises(SignInError):
            rig.google.callback({"state": begun["state"], "code": "c"})

    attempt(audience="someone-elses-client")   # not for this app
    attempt(issuer="https://evil.example")     # not Google
    attempt(nonce="the-wrong-nonce")           # replayed into another flow
    attempt(ttl=-7200)                         # expired
    attempt(verified=False)                    # unverified email
    # And nobody got an account out of all that.
    assert rig.accounts.users("t1") == []


def test_wrong_state_and_google_errors_are_refused(rig):
    with pytest.raises(SignInError, match="expired"):
        rig.google.callback({"state": "never-issued", "code": "c"})

    begun = rig.google.begin("http://127.0.0.1:8765/v1/auth/google/callback")
    with pytest.raises(SignInError, match="refused"):
        rig.google.callback({"state": begun["state"], "error": "access_denied"})
    # finish reports the failure to the app, once.
    with pytest.raises(SignInError, match="refused"):
        rig.google.finish(begun["state"])


# --------------------------------------------------------------------------- #
# Through the real gateway routes.                                             #
# --------------------------------------------------------------------------- #
def _gateway(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    # Accounts sign with the fixture IdP's own signer, so the session
    # tokens Google sign-in mints authenticate against this gateway.
    accounts = LocalAccountService(users, ident.store, ident._signer)
    links_conn = DurableConnection(tmp_path / "links.db")
    transport = FakeTransport()
    google = GoogleSignIn(
        accounts,
        IdentityLinkStore(links_conn),
        GoogleSignInConfig(client_id=CLIENT_ID),
        verifier=Hs256Verifier(GOOGLE_SECRET),
        transport=transport,
        default_tenant="t1",
    )
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        google_signin=google,
    )
    return gateway, transport, (conn, links_conn, users)


def test_the_whole_door_works_through_the_gateway(tmp_path):
    gateway, transport, closers = _gateway(tmp_path)

    # 1. The app begins (public route; redirect derived from Host).
    begun = gateway.handle(
        _req("GET", "/v1/auth/google/start", headers={"Host": "127.0.0.1:9999"})
    )
    assert begun.status == 200
    auth = urlparse(begun.body["auth_url"])
    params = parse_qs(auth.query)
    assert params["redirect_uri"] == [
        "http://127.0.0.1:9999/v1/auth/google/callback"
    ]
    state = begun.body["state"]

    # 2. Polling while the user is still at the consent screen: pending.
    pending = gateway.handle(
        _req("POST", "/v1/auth/google/finish", body={"state": state})
    )
    assert pending.body == {"status": "pending"}

    # 3. Google redirects the browser back with a code.
    transport.script(
        "oauth2.googleapis.com",
        200,
        {"id_token": _google_id_token(nonce=params["nonce"][0])},
    )
    landed = gateway.handle(
        _req(
            "GET",
            "/v1/auth/google/callback",
            query={"state": state, "code": "c1"},
        )
    )
    assert landed.status == 200
    assert landed.content_type.startswith("text/html")
    assert "Signed in as quinn" in landed.body
    assert "token" not in landed.body  # the browser never sees the session

    # 4. The app's poll now yields the session token — exactly once.
    done = gateway.handle(
        _req("POST", "/v1/auth/google/finish", body={"state": state})
    )
    assert done.body["status"] == "complete"
    assert done.body["principal"] == "quinn"
    again = gateway.handle(
        _req("POST", "/v1/auth/google/finish", body={"state": state})
    )
    assert again.status == 404

    # 5. And that token is a real session on this gateway.
    runs = gateway.handle(_req("GET", "/v1/runs", token=done.body["token"]))
    assert runs.status == 200

    for closer in closers:
        closer.close()


def test_unconfigured_hosts_answer_404_with_the_reason(tmp_path):
    app, conn, ident = _app(tmp_path)
    response = app.handle(_req("GET", "/v1/auth/google/start"))
    assert response.status == 404
    assert "not configured" in response.body["error"]["message"]
    conn.close()
