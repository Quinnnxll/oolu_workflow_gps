"""Multi-user web hosting: local accounts, same identity semantics.

`oolu host` serves the full multi-tenant gateway with this install as its
own identity provider: scrypt-hashed passwords, short-lived HS256 tokens
through the SAME `OidcValidator` path an external IdP would use, and roles
that become STORED grants — a forged token claim still buys nothing.
Login failures are uniform (no account enumeration), cost the same scrypt
work for unknown users, and repeated failures lock the username briefly.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from test_http_gateway import NOW, _app, _req

from oolu import cli
from oolu.assembly import build_host_runtime
from oolu.gateway import GatewayConfig
from oolu.identity import (
    AuthenticationError,
    Hs256Signer,
    Hs256Verifier,
    IdentityStore,
    LocalAccountService,
    LocalUserStore,
    OidcValidator,
    ProviderConfig,
    hash_password,
    verify_password,
)
from oolu.identity.accounts import LOCKOUT_THRESHOLD

SECRET = "a-thirty-two-character-plus-signing-secret"


# --------------------------------------------------------------------------- #
# Passwords.                                                                   #
# --------------------------------------------------------------------------- #
def test_password_hashes_round_trip_and_salt_uniquely():
    first, second = hash_password("correct horse"), hash_password("correct horse")
    assert first != second  # per-user random salt
    assert verify_password("correct horse", first)
    assert verify_password("correct horse", second)
    assert not verify_password("battery staple", first)


def test_password_hash_is_self_describing_and_strict():
    encoded = hash_password("longenough")
    assert encoded.startswith("scrypt$16384$8$1$")  # params ride the record
    assert not verify_password("longenough", "not-a-real-hash")
    assert not verify_password("longenough", "")
    with pytest.raises(ValueError):
        hash_password("short")  # under the minimum length


# --------------------------------------------------------------------------- #
# The account service.                                                         #
# --------------------------------------------------------------------------- #
def _service(clock=None):
    identity = IdentityStore(":memory:")
    signer = Hs256Signer(secret=SECRET, issuer="oolu-local", audience="oolu")
    service = LocalAccountService(
        LocalUserStore(":memory:"), identity, signer, clock=clock or (lambda: NOW)
    )
    return service, identity


def test_bootstrap_is_idempotent_and_never_resets_the_password():
    service, _identity = _service()
    assert (
        service.bootstrap(tenant="main", username="admin", password="first-pass")
        is True
    )
    assert (
        service.bootstrap(tenant="main", username="admin", password="other-pass")
        is False
    )
    assert service.login("admin", "first-pass").principal == "admin"
    with pytest.raises(AuthenticationError):
        service.login("admin", "other-pass")


def test_login_mints_a_token_the_gateway_validator_accepts():
    service, _identity = _service()
    service.bootstrap(tenant="main", username="admin", password="first-pass")
    result = service.login("admin", "first-pass", now=NOW)
    assert result.tenant_id == "main"
    assert result.expires_at == NOW + timedelta(hours=8)

    validator = OidcValidator(
        [
            ProviderConfig(
                issuer="oolu-local",
                audiences=frozenset({"oolu"}),
                verifier=Hs256Verifier(SECRET),
            )
        ]
    )
    claims = validator.validate(result.token, now=NOW)
    assert (claims.subject, claims.tenant_id) == ("admin", "main")
    assert claims.amr == ["pwd"]


def test_login_failures_are_uniform_no_account_enumeration():
    service, _identity = _service()
    service.bootstrap(tenant="main", username="admin", password="first-pass")
    service.create_user("mallory", "her-password", tenant="main")
    service.set_disabled("mallory", True)

    messages = set()
    for username, password in (
        ("admin", "wrong-password"),  # wrong password
        ("nobody", "any-password"),  # unknown user
        ("mallory", "her-password"),  # right password, disabled account
    ):
        with pytest.raises(AuthenticationError) as excinfo:
            service.login(username, password)
        messages.add(str(excinfo.value))
    assert messages == {"invalid credentials"}  # indistinguishable outcomes


def test_repeated_failures_lock_the_username_briefly():
    moment = NOW

    def clock():
        return moment

    service, _identity = _service(clock=clock)
    service.bootstrap(tenant="main", username="admin", password="first-pass")
    for _ in range(LOCKOUT_THRESHOLD):
        with pytest.raises(AuthenticationError):
            service.login("admin", "wrong-password")
    # Locked now: even the CORRECT password waits out the penalty...
    with pytest.raises(AuthenticationError, match="too many failed attempts"):
        service.login("admin", "first-pass")
    # ...and after it, the right password works again.
    moment = NOW + timedelta(seconds=61)
    assert service.login("admin", "first-pass").principal == "admin"


def test_roles_become_stored_grants_not_token_claims():
    service, identity = _service()
    service.bootstrap(tenant="main", username="admin", password="first-pass")
    grants = identity.list_grants("main", "admin")
    assert [g.role_name for g in grants] == ["admin"]
    role = identity.get_role("main", "admin")
    assert role is not None and "*" in role.permissions


def test_usernames_are_validated_and_duplicates_refused():
    service, _identity = _service()
    with pytest.raises(ValueError):
        service.create_user("no spaces", "longenough", tenant="main")
    with pytest.raises(ValueError):
        service.create_user("ab", "longenough", tenant="main")  # too short
    service.create_user("bob", "longenough", tenant="main")
    with pytest.raises(ValueError):
        service.create_user("bob", "otherpass", tenant="main")


# --------------------------------------------------------------------------- #
# The whole gateway: login, provision, isolate, disable.                       #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def host(tmp_path):
    runtime = build_host_runtime(data_dir=tmp_path / "host", secret=SECRET)
    runtime.accounts.bootstrap(tenant="main", username="admin", password="first-pass")
    yield runtime
    runtime.close()


def _login(runtime, username, password):
    return runtime.gateway.handle(
        _req(
            "POST",
            "/v1/auth/login",
            body={"username": username, "password": password},
        )
    )


def test_the_gateway_login_flow_end_to_end(host):
    refused = _login(host, "admin", "wrong-password")
    assert refused.status == 401

    signed_in = _login(host, "admin", "first-pass")
    assert signed_in.status == 200, signed_in.body
    token = signed_in.body["token"]
    assert signed_in.body["tenant"] == "main"

    # The token opens ordinary authenticated surfaces...
    candidates = host.gateway.handle(_req("GET", "/v1/market/candidates", token=token))
    assert candidates.status == 200, candidates.body
    # ...and stored admin authority opens user management.
    created = host.gateway.handle(
        _req(
            "POST",
            "/v1/auth/users",
            token=token,
            body={"username": "bob", "password": "bobs-password"},
        )
    )
    assert created.status == 201, created.body
    assert created.body == {
        "username": "bob",
        "roles": [],
        "disabled": False,
        "created_at": created.body["created_at"],
    }


def test_members_can_work_but_not_manage_users(host):
    admin = _login(host, "admin", "first-pass").body["token"]
    host.gateway.handle(
        _req(
            "POST",
            "/v1/auth/users",
            token=admin,
            body={"username": "bob", "password": "bobs-password"},
        )
    )
    bob = _login(host, "bob", "bobs-password").body["token"]

    assert (
        host.gateway.handle(_req("GET", "/v1/market/candidates", token=bob)).status
        == 200
    )
    forbidden = host.gateway.handle(
        _req(
            "POST",
            "/v1/auth/users",
            token=bob,
            body={"username": "eve", "password": "eves-password"},
        )
    )
    assert forbidden.status == 403  # no stored users:manage authority
    assert host.gateway.handle(_req("GET", "/v1/auth/users", token=bob)).status == 403


def test_admins_disable_users_and_disabled_users_stay_out(host):
    admin = _login(host, "admin", "first-pass").body["token"]
    host.gateway.handle(
        _req(
            "POST",
            "/v1/auth/users",
            token=admin,
            body={"username": "bob", "password": "bobs-password"},
        )
    )
    disabled = host.gateway.handle(
        _req(
            "POST",
            "/v1/auth/users/bob/disabled",
            token=admin,
            body={"disabled": True},
        )
    )
    assert disabled.status == 200 and disabled.body["disabled"] is True
    assert _login(host, "bob", "bobs-password").status == 401

    listing = host.gateway.handle(_req("GET", "/v1/auth/users", token=admin))
    assert {u["username"]: u["disabled"] for u in listing.body["items"]} == {
        "admin": False,
        "bob": True,
    }
    missing = host.gateway.handle(
        _req(
            "POST",
            "/v1/auth/users/ghost/disabled",
            token=admin,
            body={"disabled": True},
        )
    )
    assert missing.status == 404


def test_login_is_404_when_local_accounts_are_not_configured(tmp_path):
    from test_http_gateway import _app

    gateway, conn, _ident = _app(tmp_path)
    response = gateway.handle(
        _req(
            "POST",
            "/v1/auth/login",
            body={"username": "admin", "password": "whatever-long"},
        )
    )
    assert response.status == 404  # IdP-fronted installs lose nothing
    conn.close()


def test_host_runtime_refuses_a_short_secret(tmp_path):
    with pytest.raises(ValueError):
        build_host_runtime(data_dir=tmp_path, secret="too-short")


# --------------------------------------------------------------------------- #
# The `oolu host` command.                                                    #
# --------------------------------------------------------------------------- #
def _run_host(monkeypatch, tmp_path, *, secret=SECRET, admin_password="first-pass"):
    import uvicorn

    served = {}

    def fake_run(app, **kwargs):
        served["app"] = app
        served.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    for name, value in (
        ("OOLU_HOST_SECRET", secret),
        ("OOLU_ADMIN_PASSWORD", admin_password),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    out = io.StringIO()
    code = cli.main(["host", "--data", str(tmp_path / "data")], out=out)
    return code, out.getvalue(), served


def test_oolu_host_serves_the_gateway_asgi(monkeypatch, tmp_path):
    from oolu.gateway.asgi import GatewayASGI

    code, banner, served = _run_host(monkeypatch, tmp_path)
    assert code == 0
    assert isinstance(served["app"], GatewayASGI)
    assert (served["host"], served["port"]) == ("0.0.0.0", 8788)
    assert "/v1/auth/login" in banner and '"username": "admin"' in banner
    assert "HTTPS" in banner
    assert "sqlite" in banner  # the default durable backend


def test_oolu_host_generates_credentials_once_when_unset(monkeypatch, tmp_path):
    code, banner, _served = _run_host(
        monkeypatch, tmp_path, secret=None, admin_password=None
    )
    assert code == 0
    assert "shown ONCE" in banner  # the generated admin password
    assert "ephemeral" in banner  # and the unset-secret consequence


def test_oolu_host_refuses_a_short_secret(monkeypatch, tmp_path, capsys):
    code, _banner, served = _run_host(monkeypatch, tmp_path, secret="short")
    assert code == 2 and "app" not in served
    assert "32 characters" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Online database + cross-origin desktop clients.                             #
# --------------------------------------------------------------------------- #
def test_gateway_applies_configured_cors_origins(tmp_path):
    """`--allow-origin` becomes real CORS: only listed origins are echoed."""
    app, conn, _ident = _app(
        tmp_path,
        config=GatewayConfig(allowed_origins=frozenset({"https://app.example"})),
    )
    try:
        allowed = app.handle(
            _req("OPTIONS", "/v1/runs", headers={"Origin": "https://app.example"})
        )
        assert allowed.headers["Access-Control-Allow-Origin"] == "https://app.example"
        assert "Authorization" in allowed.headers["Access-Control-Allow-Headers"]

        blocked = app.handle(
            _req("OPTIONS", "/v1/runs", headers={"Origin": "https://evil.example"})
        )
        assert "Access-Control-Allow-Origin" not in blocked.headers
    finally:
        conn.close()


def test_oolu_host_passes_database_url_and_cors_to_runtime(monkeypatch, tmp_path):
    """`oolu host` forwards --database-url / --allow-origin to the runtime."""
    import uvicorn

    import oolu.assembly as assembly

    captured: dict = {}

    class _FakeAccounts:
        def bootstrap(self, **_kwargs):
            return True

    class _FakeRuntime:
        accounts = _FakeAccounts()
        asgi = object()

        def close(self):
            pass

    def spy(settings=None, **kwargs):
        captured.update(kwargs)
        return _FakeRuntime()

    monkeypatch.setattr(assembly, "build_host_runtime", spy)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    monkeypatch.setenv("OOLU_HOST_SECRET", SECRET)
    monkeypatch.setenv("OOLU_ADMIN_PASSWORD", "first-pass")

    out = io.StringIO()
    code = cli.main(
        [
            "host",
            "--data",
            str(tmp_path / "data"),
            "--database-url",
            "postgresql://u:p@db.example/oolu",
            "--allow-origin",
            "https://app.example",
            "--allow-origin",
            "https://alt.example",
        ],
        out=out,
    )
    assert code == 0
    assert captured["database_url"] == "postgresql://u:p@db.example/oolu"
    assert captured["config"].allowed_origins == frozenset(
        {"https://app.example", "https://alt.example"}
    )
    assert "postgres (online)" in out.getvalue()
