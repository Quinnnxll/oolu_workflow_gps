"""Going online: the client-config door and self-serve registration.

client-config is how an install stops asking the user for a server: the
desktop reads its paired online server (OOLU_SERVER_URL) from its own
gateway, and the sign-in screen shows no Server field. Registration is the
online host's opt-in door; the e-mail is recorded as an identity link so
one address registers once — verification codes arrive with the
mail-sender milestone.
"""

from __future__ import annotations

from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp, GatewayConfig
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.identity.google_signin import IdentityLinkStore


def _host(tmp_path, **config_kwargs):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    links_conn = DurableConnection(tmp_path / "links.db")
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        identity_links=IdentityLinkStore(links_conn),
        config=GatewayConfig(registration_tenant="t1", **config_kwargs),
    )
    return gateway, accounts, (conn, links_conn, users)


def test_client_config_is_public_and_honest_by_default(tmp_path):
    app, conn, _ = _app(tmp_path)  # bare gateway: nothing configured
    response = app.handle(_req("GET", "/v1/client-config"))
    assert response.status == 200
    assert response.body == {
        "server": None,
        "google": False,
        "registration": False,
        "verification": False,
    }
    conn.close()


def test_client_config_advertises_the_paired_server_and_doors(tmp_path):
    gateway, _, closers = _host(
        tmp_path,
        server_url="https://cloud.oolu.example",
        open_registration=True,
    )
    response = gateway.handle(_req("GET", "/v1/client-config"))
    assert response.body["server"] == "https://cloud.oolu.example"
    assert response.body["registration"] is True
    assert response.body["google"] is False  # no client configured here
    for closer in closers:
        closer.close()


def test_registration_is_closed_unless_the_host_opts_in(tmp_path):
    gateway, _, closers = _host(tmp_path)  # open_registration defaults off
    response = gateway.handle(
        _req(
            "POST",
            "/v1/auth/register",
            body={"email": "quinn@mphepo.io", "password": "long-enough-pw"},
        )
    )
    assert response.status == 404
    assert "not open" in response.body["error"]["message"]
    for closer in closers:
        closer.close()


def test_register_creates_an_account_and_the_token_works(tmp_path):
    gateway, accounts, closers = _host(tmp_path, open_registration=True)

    created = gateway.handle(
        _req(
            "POST",
            "/v1/auth/register",
            body={"email": "Quinn@Mphepo.io", "password": "long-enough-pw"},
        )
    )
    assert created.status == 201
    assert created.body["principal"] == "quinn"
    assert created.body["tenant"] == "t1"

    # The token is a real session on this gateway.
    runs = gateway.handle(_req("GET", "/v1/runs", token=created.body["token"]))
    assert runs.status == 200
    # And the password is the real credential from now on.
    assert accounts.login("quinn", "long-enough-pw").principal == "quinn"

    for closer in closers:
        closer.close()


def test_an_email_registers_exactly_once(tmp_path):
    gateway, _, closers = _host(tmp_path, open_registration=True)
    body = {"email": "quinn@mphepo.io", "password": "long-enough-pw"}
    assert gateway.handle(_req("POST", "/v1/auth/register", body=body)).status == 201

    again = gateway.handle(
        _req(
            "POST",
            "/v1/auth/register",
            body={**body, "password": "another-password"},
        )
    )
    assert again.status == 409
    assert "already registered" in again.body["error"]["message"]
    for closer in closers:
        closer.close()


def test_username_collisions_get_suffixes(tmp_path):
    gateway, accounts, closers = _host(tmp_path, open_registration=True)
    accounts.create_user("quinn", "quinns-own-password", tenant="t1")

    created = gateway.handle(
        _req(
            "POST",
            "/v1/auth/register",
            body={"email": "quinn@elsewhere.example", "password": "long-enough-pw"},
        )
    )
    assert created.body["principal"] == "quinn-2"
    # The original password account is untouched.
    assert accounts.login("quinn", "quinns-own-password").principal == "quinn"
    for closer in closers:
        closer.close()


def test_junk_is_refused(tmp_path):
    gateway, _, closers = _host(tmp_path, open_registration=True)
    bad_email = gateway.handle(
        _req(
            "POST",
            "/v1/auth/register",
            body={"email": "not-an-email", "password": "long-enough-pw"},
        )
    )
    assert bad_email.status == 400
    short_pw = gateway.handle(
        _req(
            "POST",
            "/v1/auth/register",
            body={"email": "quinn@mphepo.io", "password": "short"},
        )
    )
    assert short_pw.status == 400
    for closer in closers:
        closer.close()
