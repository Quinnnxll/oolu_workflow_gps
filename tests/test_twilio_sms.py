"""The phone door that actually reaches a real provider.

'Registration through phone number with SMS verification code is not
functional' had one honest cause: the generic SMS sender speaks JSON +
Bearer, and Twilio — the provider almost everyone reaches for — speaks
form-encoded + HTTP Basic against a per-account message resource, and
401s the other shape. ``TwilioSmsSender`` is that provider, and
``build_sms_sender`` now picks it whenever Twilio is configured (or
asked for), so a deployer who sets the Twilio variables gets a working
phone door instead of a silent 401.
"""

from __future__ import annotations

import pytest

from oolu.sms import (
    ConsoleSmsSender,
    HttpSmsSender,
    TwilioSmsSender,
    build_sms_sender,
)


class _RecordingTransport:
    """Captures one POST and answers with a scripted status/body."""

    def __init__(self, status: int = 201, payload: dict | None = None):
        self._status = status
        self._payload = payload or {}
        self.calls: list[dict] = []

    def post(self, url, *, data=None, json=None, auth=None, headers=None):
        self.calls.append(
            {
                "url": url,
                "data": data,
                "json": json,
                "auth": auth,
                "headers": headers,
            }
        )
        return _Resp(self._status, self._payload)


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# The Twilio wire shape: form-encoded body, Basic auth, the message resource.  #
# --------------------------------------------------------------------------- #
def test_twilio_sends_form_encoded_with_basic_auth():
    transport = _RecordingTransport(status=201)
    sender = TwilioSmsSender(
        account_sid="AC123",
        auth_token="tok-secret",
        sender="+15550100000",
        transport=transport,
    )
    sender.send(to="+15550109999", body="Your OoLu sign-in code is 424242.")
    [call] = transport.calls
    # The endpoint is derived from the account SID — the real Twilio path.
    assert call["url"] == (
        "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    )
    # Form fields, capitalized the way Twilio wants; a plain number is From.
    assert call["data"] == {
        "To": "+15550109999",
        "Body": "Your OoLu sign-in code is 424242.",
        "From": "+15550100000",
    }
    assert "json" not in call or call["json"] is None
    # HTTP Basic auth: SID as user, token as pass.
    assert call["auth"] == ("AC123", "tok-secret")
    assert "form-urlencoded" in call["headers"]["Content-Type"]


def test_a_messaging_service_sid_goes_in_its_own_field():
    transport = _RecordingTransport()
    sender = TwilioSmsSender(
        account_sid="AC1",
        auth_token="t",
        sender="MG999",  # a Messaging Service SID, not a number
        transport=transport,
    )
    sender.send(to="+15550100000", body="hi")
    data = transport.calls[0]["data"]
    assert data["MessagingServiceSid"] == "MG999"
    assert "From" not in data


def test_a_twilio_failure_surfaces_the_providers_own_reason():
    transport = _RecordingTransport(
        status=400, payload={"message": "The 'From' number is not a valid phone number"}
    )
    sender = TwilioSmsSender(
        account_sid="AC1", auth_token="t", sender="+1", transport=transport
    )
    with pytest.raises(RuntimeError) as exc:
        sender.send(to="+15550100000", body="hi")
    assert "400" in str(exc.value)
    assert "not a valid phone number" in str(exc.value)


# --------------------------------------------------------------------------- #
# The builder picks the right door from the environment.                       #
# --------------------------------------------------------------------------- #
def test_twilio_is_chosen_when_its_credentials_are_present():
    sender = build_sms_sender(
        {
            "OOLU_TWILIO_ACCOUNT_SID": "AC1",
            "OOLU_TWILIO_AUTH_TOKEN": "tok",
            "OOLU_SMS_FROM": "+15550100000",
        }
    )
    assert isinstance(sender, TwilioSmsSender)


def test_provider_twilio_selects_it_and_incomplete_config_is_loud():
    # Explicitly asked for, fully configured -> Twilio.
    sender = build_sms_sender(
        {
            "OOLU_SMS_PROVIDER": "twilio",
            "OOLU_TWILIO_ACCOUNT_SID": "AC1",
            "OOLU_TWILIO_AUTH_TOKEN": "tok",
            "OOLU_SMS_FROM": "+15550100000",
        }
    )
    assert isinstance(sender, TwilioSmsSender)
    # Asked for but half-configured -> a loud refusal, never a silent
    # fallback to a door Twilio cannot answer.
    with pytest.raises(ValueError, match="Twilio SMS is selected but incomplete"):
        build_sms_sender({"OOLU_SMS_PROVIDER": "twilio", "OOLU_SMS_FROM": "+1"})


def test_a_twilio_url_in_the_generic_slot_still_picks_twilio():
    sender = build_sms_sender(
        {
            "OOLU_SMS_URL": "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json",
            "OOLU_SMS_KEY": "AC1:tok",  # sid:token in the generic key slot
            "OOLU_SMS_FROM": "+15550100000",
        }
    )
    assert isinstance(sender, TwilioSmsSender)


def test_the_generic_and_console_doors_still_work():
    assert isinstance(build_sms_sender({"OOLU_SMS": "console"}), ConsoleSmsSender)
    generic = build_sms_sender(
        {
            "OOLU_SMS_URL": "https://sms.example/send",
            "OOLU_SMS_KEY": "k",
            "OOLU_SMS_FROM": "+15550100000",
        }
    )
    assert isinstance(generic, HttpSmsSender)
    assert build_sms_sender({}) is None
