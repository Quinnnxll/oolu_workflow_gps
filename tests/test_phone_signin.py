"""Issue 11: continue with phone, open registration, message non-friends.

A texted code is the key: it signs an existing number in and CREATES the
account for a new one — born with a usable auto-generated password,
texted to its owner, changeable in Settings. Registration is open by
default (a server exists to take accounts). And a manual registration
can never squat the reserved phone-… username namespace.
"""

from __future__ import annotations

import pytest
from test_friend_requests import _host
from test_http_gateway import _req

from oolu.mail import MailCodeStore
from oolu.sms import RecordingSmsSender, normalize_phone


def _phone_host(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    sms = RecordingSmsSender()
    gateway._sms = sms
    gateway._mail_codes = MailCodeStore(conn)
    return gateway, conn, ident, sms


# --------------------------------------------------------------------------- #
# The number in canonical form.                                                #
# --------------------------------------------------------------------------- #
def test_phone_numbers_normalize_or_refuse():
    assert normalize_phone("+1 (555) 010-0000") == "+15550100000"
    assert normalize_phone("15550100000") == "+15550100000"
    for bad in ("", "555", "+1 555 CALL ME", "1" * 20):
        with pytest.raises(ValueError):
            normalize_phone(bad)


# --------------------------------------------------------------------------- #
# The flow: code in, account out.                                              #
# --------------------------------------------------------------------------- #
def test_a_new_number_gets_an_account_and_a_texted_password(tmp_path):
    gateway, conn, ident, sms = _phone_host(tmp_path)
    try:
        started = gateway.handle(
            _req("POST", "/v1/auth/phone/start", body={"phone": "+1 555 010 0000"})
        )
        assert started.status == 200 and started.body == {"sent": True}
        [text] = sms.sent
        assert text["to"] == "+15550100000"
        code = next(
            w.strip(".")
            for w in text["body"].split()
            if w.strip(".").isdigit() and len(w.strip(".")) == 6
        )

        # A wrong code is refused; the right one creates and signs in.
        wrong = gateway.handle(
            _req("POST", "/v1/auth/phone/verify",
                 body={"phone": "+15550100000", "code": "000000"})
        )
        assert wrong.status == 401
        verified = gateway.handle(
            _req("POST", "/v1/auth/phone/verify",
                 body={"phone": "+1 (555) 010-0000", "code": code})
        )
        assert verified.status == 200, verified.body
        assert verified.body["created"] is True
        assert verified.body["token"]
        username = verified.body["principal"]
        assert username.startswith("phone-")
        # The account was born WITH a usable password, texted over.
        welcome = sms.sent[-1]
        assert username in welcome["body"] and "password" in welcome["body"]
        password = welcome["body"].split("password is ")[1].split(" ")[0]
        login = gateway.handle(
            _req("POST", "/v1/auth/login",
                 body={"username": username, "password": password})
        )
        assert login.status == 200, login.body

        # The SAME number next time: sign-in, never a second account.
        # (Past the send cooldown — a second text moments later is paced.)
        from datetime import timedelta

        from test_http_gateway import NOW

        later = NOW + timedelta(minutes=2)
        code2_start = gateway.handle(
            _req(
                "POST",
                "/v1/auth/phone/start",
                body={"phone": "+15550100000"},
                now=later,
            )
        )
        assert code2_start.status == 200
        code2 = next(
            w.strip(".")
            for w in sms.sent[-1]["body"].split()
            if w.strip(".").isdigit() and len(w.strip(".")) == 6
        )
        again = gateway.handle(
            _req("POST", "/v1/auth/phone/verify",
                 body={"phone": "+15550100000", "code": code2})
        )
        assert again.status == 200 and again.body["created"] is False
        assert again.body["principal"] == username
        # A code is single-use.
        replay = gateway.handle(
            _req("POST", "/v1/auth/phone/verify",
                 body={"phone": "+15550100000", "code": code2})
        )
        assert replay.status == 401
    finally:
        conn.close()


def test_no_sms_door_answers_404(tmp_path):
    gateway, conn, ident = _host(tmp_path)  # no sms sender wired
    try:
        assert gateway.handle(
            _req("POST", "/v1/auth/phone/start", body={"phone": "+15550100000"})
        ).status == 404
    finally:
        conn.close()


def test_manual_registration_never_squats_the_phone_namespace(tmp_path):
    """The account-creation rule: an e-mail whose local part collides
    with the reserved phone-… namespace gets a prefixed username, so
    "continue with phone" always finds its own name free."""
    gateway, conn, ident, sms = _phone_host(tmp_path)
    try:
        from oolu.gateway import GatewayApp

        class _Accounts:
            def __init__(self):
                self.taken: set[str] = set()

            def user(self, name):
                return name if name in self.taken else None

        accounts = _Accounts()
        assert GatewayApp._fresh_username("phone-0000@example.com", accounts) == (
            "u-phone-0000"
        )
        assert GatewayApp._fresh_username("alice@example.com", accounts) == "alice"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Registration is open by default; messaging does not wait on friendship.      #
# --------------------------------------------------------------------------- #
def test_registration_is_open_by_default(tmp_path):
    from oolu.gateway import GatewayConfig

    assert GatewayConfig().open_registration is True
    gateway, conn, ident = _host(tmp_path)
    try:
        created = gateway.handle(
            _req("POST", "/v1/auth/register",
                 body={"email": "carol@example.com", "password": "carol-pass-1"})
        )
        assert created.status == 201, created.body
        assert created.body["principal"] == "carol"
    finally:
        conn.close()


def test_a_stranger_can_message_an_open_recipient(tmp_path):
    """The 'receive messages from non-friends' setting is functional:
    the default-open recipient hears strangers; flipping it off turns
    the same send into the friend-request nudge."""
    gateway, conn, ident = _host(tmp_path)
    try:
        alice, bob = ident.token("alice", "t1"), ident.token("bob", "t1")
        sent = gateway.handle(
            _req("POST", "/v1/friends/bob/messages", token=alice,
                 body={"text": "hello from a stranger"})
        )
        assert sent.status == 201, sent.body
        # Bob closes the door: the next stranger message nudges instead.
        gateway.handle(
            _req("PUT", "/v1/friends/settings", token=bob,
                 body={"allow_nonfriend_messages": False})
        )
        refused = gateway.handle(
            _req("POST", "/v1/friends/bob/messages", token=alice,
                 body={"text": "again"})
        )
        assert refused.status == 403
        assert "friend request" in refused.body["error"]["message"]
    finally:
        conn.close()
