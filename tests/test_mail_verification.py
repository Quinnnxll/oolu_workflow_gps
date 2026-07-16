"""Going public: e-mail verification and password reset.

A public host must prove that whoever registers controls the address they
typed (verification codes), and must offer a way back in without support
tickets (reset codes). Exit gate: registration answers without a token
until the code comes back; unverified accounts cannot sign in; codes are
single-use, expiring, and attempt-limited; reset never enumerates
accounts; and a public host (`--global-service`) refuses to run
synthesized code without real isolation — or to open registration without
a mail sender.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from test_http_gateway import _app, _req

from oolu.durable.connection import DurableConnection
from oolu.gateway import GatewayApp, GatewayConfig
from oolu.identity import LocalAccountService, LocalUserStore
from oolu.identity.google_signin import IdentityLinkStore
from oolu.mail import (
    MAX_ATTEMPTS,
    ConsoleMailSender,
    HttpMailSender,
    MailCodeStore,
    RecordingMailSender,
    build_mail_sender,
)

EMAIL = "quinn@mphepo.io"
PASSWORD = "long-enough-pw"


def _host(tmp_path):
    """The registration rig from test_client_config_register, plus mail."""
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    links_conn = DurableConnection(tmp_path / "links.db")
    outbox = RecordingMailSender()
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        identity_links=IdentityLinkStore(links_conn),
        mail=outbox,
        mail_codes=MailCodeStore(links_conn),
        config=GatewayConfig(registration_tenant="t1", open_registration=True),
    )
    return gateway, outbox, (conn, links_conn, users)


def _register(gateway, email=EMAIL, password=PASSWORD):
    return gateway.handle(
        _req("POST", "/v1/auth/register", body={"email": email, "password": password})
    )


def _login(gateway, username, password):
    return gateway.handle(
        _req(
            "POST",
            "/v1/auth/login",
            body={"username": username, "password": password},
        )
    )


def _code_in(mail: dict) -> str:
    match = re.search(r"\b(\d{6})\b", mail["body"])
    assert match, mail["body"]
    return match.group(1)


# --------------------------------------------------------------------------- #
# Registration becomes verification-first when a mail sender exists.           #
# --------------------------------------------------------------------------- #
def test_register_mails_a_code_and_withholds_the_token(tmp_path):
    gateway, outbox, closers = _host(tmp_path)

    created = _register(gateway)
    assert created.status == 201
    assert created.body == {"verification_required": True, "email": EMAIL}
    assert "token" not in created.body  # nothing to steal before the proof

    assert len(outbox.sent) == 1
    assert outbox.sent[0]["to"] == EMAIL
    assert len(_code_in(outbox.sent[0])) == 6

    # And the sign-in screen can know in advance the code step is coming.
    config = gateway.handle(_req("GET", "/v1/client-config"))
    assert config.body["verification"] is True
    for closer in closers:
        closer.close()


def test_unverified_accounts_cannot_sign_in(tmp_path):
    gateway, _, closers = _host(tmp_path)
    _register(gateway)

    refused = _login(gateway, "quinn", PASSWORD)
    assert refused.status == 403
    assert refused.body["error"]["code"] == "verification_required"
    for closer in closers:
        closer.close()


def test_the_mailed_code_plus_the_password_open_the_door(tmp_path):
    gateway, outbox, closers = _host(tmp_path)
    _register(gateway)
    code = _code_in(outbox.sent[0])

    verified = gateway.handle(
        _req(
            "POST",
            "/v1/auth/verify",
            body={"email": EMAIL, "code": code, "password": PASSWORD},
        )
    )
    assert verified.status == 200, verified.body
    assert verified.body["principal"] == "quinn"
    assert verified.body["token"]

    # From now on plain sign-in works.
    assert _login(gateway, "quinn", PASSWORD).status == 200
    for closer in closers:
        closer.close()


def test_the_code_alone_is_not_a_session(tmp_path):
    """A leaked inbox is not a leaked account: the password rides along."""
    gateway, outbox, closers = _host(tmp_path)
    _register(gateway)
    code = _code_in(outbox.sent[0])

    wrong_pw = gateway.handle(
        _req(
            "POST",
            "/v1/auth/verify",
            body={"email": EMAIL, "code": code, "password": "not-the-password"},
        )
    )
    assert wrong_pw.status == 401
    # The code DID prove the address, so the real password now signs in.
    assert _login(gateway, "quinn", PASSWORD).status == 200
    for closer in closers:
        closer.close()


def test_wrong_codes_fail_and_burn_out(tmp_path):
    gateway, outbox, closers = _host(tmp_path)
    _register(gateway)
    real = _code_in(outbox.sent[0])
    wrong = "000000" if real != "000000" else "111111"

    for _ in range(MAX_ATTEMPTS):
        attempt = gateway.handle(
            _req(
                "POST",
                "/v1/auth/verify",
                body={"email": EMAIL, "code": wrong, "password": PASSWORD},
            )
        )
        assert attempt.status == 400

    # Guessing exhausted the code: even the real one is dead now.
    spent = gateway.handle(
        _req(
            "POST",
            "/v1/auth/verify",
            body={"email": EMAIL, "code": real, "password": PASSWORD},
        )
    )
    assert spent.status == 400
    for closer in closers:
        closer.close()


# --------------------------------------------------------------------------- #
# Password reset.                                                              #
# --------------------------------------------------------------------------- #
def test_reset_never_enumerates_accounts(tmp_path):
    gateway, outbox, closers = _host(tmp_path)
    _register(gateway)
    outbox.sent.clear()

    unknown = gateway.handle(
        _req(
            "POST",
            "/v1/auth/reset/request",
            body={"email": "nobody@mphepo.io"},
        )
    )
    known = gateway.handle(
        _req("POST", "/v1/auth/reset/request", body={"email": EMAIL})
    )
    # Same answer either way; only the real account got a mail.
    assert unknown.status == known.status == 202
    assert unknown.body == known.body == {"status": "sent"}
    assert [mail["to"] for mail in outbox.sent] == [EMAIL]
    for closer in closers:
        closer.close()


def test_reset_changes_the_password_and_counts_as_verification(tmp_path):
    gateway, outbox, closers = _host(tmp_path)
    _register(gateway)  # never verified — then the user forgets the password
    outbox.sent.clear()

    gateway.handle(_req("POST", "/v1/auth/reset/request", body={"email": EMAIL}))
    code = _code_in(outbox.sent[0])
    changed = gateway.handle(
        _req(
            "POST",
            "/v1/auth/reset/confirm",
            body={"email": EMAIL, "code": code, "password": "brand-new-password"},
        )
    )
    assert changed.status == 200
    assert changed.body == {"status": "password_changed"}

    # The old password is gone, the new one works — and proving inbox
    # control counted as e-mail verification, so sign-in is open.
    assert _login(gateway, "quinn", PASSWORD).status == 401
    assert _login(gateway, "quinn", "brand-new-password").status == 200
    for closer in closers:
        closer.close()


def test_reset_confirm_refuses_junk(tmp_path):
    gateway, outbox, closers = _host(tmp_path)
    _register(gateway)
    outbox.sent.clear()
    gateway.handle(_req("POST", "/v1/auth/reset/request", body={"email": EMAIL}))
    code = _code_in(outbox.sent[0])

    short = gateway.handle(
        _req(
            "POST",
            "/v1/auth/reset/confirm",
            body={"email": EMAIL, "code": code, "password": "short"},
        )
    )
    assert short.status == 400
    wrong = gateway.handle(
        _req(
            "POST",
            "/v1/auth/reset/confirm",
            body={"email": EMAIL, "code": "999999", "password": "a-fine-password"},
        )
    )
    assert wrong.status == 400
    for closer in closers:
        closer.close()


# --------------------------------------------------------------------------- #
# Forgot password, one step: the server e-mails a fresh password.              #
# --------------------------------------------------------------------------- #
def _password_in(mail: dict) -> str:
    match = re.search(r"new\s+password:\s*(\S+)", mail["body"])
    assert match, mail["body"]
    return match.group(1)


def test_email_new_password_sets_it_and_signs_in(tmp_path):
    gateway, outbox, closers = _host(tmp_path)
    _register(gateway)  # never verified — then the user forgets the password
    outbox.sent.clear()

    sent = gateway.handle(
        _req("POST", "/v1/auth/reset/password", body={"email": EMAIL})
    )
    assert sent.status == 202
    assert sent.body == {"status": "sent"}
    assert [m["to"] for m in outbox.sent] == [EMAIL]
    new_password = _password_in(outbox.sent[0])

    # The old password is dead; the e-mailed one works — and receiving it
    # counted as verification, so the door is open even though the account
    # was never verified before.
    assert _login(gateway, "quinn", PASSWORD).status == 401
    assert _login(gateway, "quinn", new_password).status == 200
    for closer in closers:
        closer.close()


def test_email_new_password_never_enumerates_accounts(tmp_path):
    gateway, outbox, closers = _host(tmp_path)
    _register(gateway)
    outbox.sent.clear()

    unknown = gateway.handle(
        _req("POST", "/v1/auth/reset/password", body={"email": "nobody@mphepo.io"})
    )
    known = gateway.handle(
        _req("POST", "/v1/auth/reset/password", body={"email": EMAIL})
    )
    assert unknown.status == known.status == 202
    assert unknown.body == known.body == {"status": "sent"}
    # Only the real account got a mail; the unknown address got silence.
    assert [m["to"] for m in outbox.sent] == [EMAIL]
    for closer in closers:
        closer.close()


def test_email_new_password_needs_a_mail_sender(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    # A host with accounts but no mail door: the route is honestly 404.
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        config=GatewayConfig(registration_tenant="t1", open_registration=True),
    )
    resp = gateway.handle(
        _req("POST", "/v1/auth/reset/password", body={"email": EMAIL})
    )
    assert resp.status == 404
    conn.close()


# --------------------------------------------------------------------------- #
# The code store itself.                                                       #
# --------------------------------------------------------------------------- #
def test_codes_expire_and_are_single_use(tmp_path):
    now = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    clock = {"now": now}
    conn = DurableConnection(tmp_path / "codes.db")
    store = MailCodeStore(conn, clock=lambda: clock["now"])

    code = store.issue(EMAIL, "verify")
    clock["now"] = now + timedelta(minutes=31)
    assert not store.redeem(EMAIL, "verify", code)  # expired

    code = store.issue(EMAIL, "verify")
    assert store.redeem(EMAIL, "verify", code)
    assert store.is_verified(EMAIL, "verify")
    assert not store.redeem(EMAIL, "verify", code)  # burned: single-use

    # Purposes are separate ledgers: verify says nothing about reset.
    assert not store.is_verified(EMAIL, "reset")
    conn.close()


def test_mark_verified_without_a_code(tmp_path):
    conn = DurableConnection(tmp_path / "codes.db")
    store = MailCodeStore(conn)
    assert not store.is_verified(EMAIL, "verify")
    store.mark_verified(EMAIL, "verify")
    assert store.is_verified(EMAIL, "verify")
    conn.close()


# --------------------------------------------------------------------------- #
# The outbound door.                                                           #
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, status):
        self.status = status


class _Transport:
    def __init__(self, status=200):
        self.status = status
        self.requests: list[dict] = []

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers or {}), "body": body}
        )
        return _Response(self.status)


def test_http_mail_sender_speaks_the_resend_shape():
    transport = _Transport()
    sender = HttpMailSender(
        url="https://api.resend.example/emails",
        api_key="re_secret",
        sender="OoLu <hello@oolu.example>",
        transport=transport,
    )
    sender.send(to=EMAIL, subject="Your code", body="123456")

    [request] = transport.requests
    assert request["method"] == "POST"
    assert request["headers"]["Authorization"] == "Bearer re_secret"
    assert request["body"] == {
        "from": "OoLu <hello@oolu.example>",
        "to": [EMAIL],
        "subject": "Your code",
        "text": "123456",
    }


def test_http_mail_sender_raises_on_refusal():
    sender = HttpMailSender(
        url="https://api.resend.example/emails",
        api_key="re_secret",
        sender="hello@oolu.example",
        transport=_Transport(status=401),
    )
    try:
        sender.send(to=EMAIL, subject="s", body="b")
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("a refused send must raise")


def test_build_mail_sender_reads_the_environment():
    assert build_mail_sender({}) is None
    assert isinstance(build_mail_sender({"OOLU_MAIL": "console"}), ConsoleMailSender)
    http_env = {
        "OOLU_MAIL_URL": "https://api.resend.example/emails",
        "OOLU_MAIL_KEY": "re_secret",
        "OOLU_MAIL_FROM": "hello@oolu.example",
    }
    assert isinstance(build_mail_sender(http_env), HttpMailSender)
    # All three or nothing: a half-configured door stays shut.
    assert build_mail_sender({"OOLU_MAIL_URL": "https://x.example"}) is None


# --------------------------------------------------------------------------- #
# The public-host walls.                                                       #
# --------------------------------------------------------------------------- #
def test_require_isolation_keeps_the_script_hand_off(tmp_path, caplog):
    """A public host with only the subprocess dev backend must never wire
    the script hand: synthesized code doesn't run unsandboxed in public."""
    import logging

    from oolu.assembly import build_host_runtime

    secret = "a-thirty-two-character-plus-signing-secret"
    with caplog.at_level(logging.WARNING, logger="oolu.assembly"):
        runtime = build_host_runtime(
            data_dir=tmp_path / "host", secret=secret, require_isolation=True
        )
    runtime.close()
    assert any("script hand stays OFF" in r.message for r in caplog.records)


def test_global_service_registration_requires_a_mail_sender(
    tmp_path, monkeypatch, capsys
):
    from oolu import cli

    for var in ("OOLU_MAIL", "OOLU_MAIL_URL", "OOLU_MAIL_KEY", "OOLU_MAIL_FROM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(
        "OOLU_HOST_SECRET", "a-thirty-two-character-plus-signing-secret"
    )
    code = cli.main(
        [
            "host",
            "--data",
            str(tmp_path / "data"),
            "--global-service",
            "--open-registration",
        ]
    )
    assert code != 0
    assert "mail sender" in capsys.readouterr().err
