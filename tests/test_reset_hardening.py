"""The forgot-password hardening, unit-close.

Two pieces, tested with an injected clock so time-dependent behavior is
deterministic:

- ``PendingPasswordStore`` — the e-mailed password waits beside the real
  one, promotes exactly once on first use, expires, and is clearable
  (which is how an owner's ordinary sign-in dispels a stranger's staged
  key).
- ``SendThrottle`` — the outbound doors (reset mail, sign-in SMS) are
  paced per identifier and purpose: a cooldown between sends and a daily
  cap, so neither becomes a mail cannon nor a billing lever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oolu.durable import DurableConnection
from oolu.identity.accounts import PendingPasswordStore
from oolu.mail import SendThrottle

T0 = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The staged password.                                                         #
# --------------------------------------------------------------------------- #
def test_a_staged_password_promotes_exactly_once(tmp_path):
    clock = {"now": T0}
    conn = DurableConnection(tmp_path / "p.db")
    try:
        store = PendingPasswordStore(conn, clock=lambda: clock["now"])
        store.stage("alice", "brand-new-pass")
        # The wrong guess never spends the key; the right one spends it once.
        assert store.take("alice", "not-it") is False
        assert store.take("alice", "brand-new-pass") is True
        assert store.take("alice", "brand-new-pass") is False  # single-use
    finally:
        conn.close()


def test_a_staged_password_expires(tmp_path):
    clock = {"now": T0}
    conn = DurableConnection(tmp_path / "p.db")
    try:
        store = PendingPasswordStore(conn, clock=lambda: clock["now"])
        store.stage("alice", "brand-new-pass")
        clock["now"] = T0 + timedelta(minutes=31)
        assert store.take("alice", "brand-new-pass") is False  # too late
    finally:
        conn.close()


def test_staging_again_replaces_the_prior_key(tmp_path):
    conn = DurableConnection(tmp_path / "p.db")
    try:
        store = PendingPasswordStore(conn, clock=lambda: T0)
        store.stage("alice", "first-staged-pw")
        store.stage("alice", "second-staged-pw")
        assert store.take("alice", "first-staged-pw") is False
        assert store.take("alice", "second-staged-pw") is True
    finally:
        conn.close()


def test_clear_dispels_a_staged_key(tmp_path):
    conn = DurableConnection(tmp_path / "p.db")
    try:
        store = PendingPasswordStore(conn, clock=lambda: T0)
        store.stage("alice", "brand-new-pass")
        store.clear("alice")  # the owner's ordinary sign-in does this
        assert store.take("alice", "brand-new-pass") is False
        store.clear("bob")  # clearing a nonexistent key is a no-op
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The send throttle.                                                           #
# --------------------------------------------------------------------------- #
def test_the_cooldown_paces_back_to_back_sends(tmp_path):
    clock = {"now": T0}
    conn = DurableConnection(tmp_path / "t.db")
    try:
        throttle = SendThrottle(conn, clock=lambda: clock["now"])

        def allow():
            return throttle.allow(
                "quinn@mphepo.io", "reset", cooldown_s=60, per_day=5
            )

        assert allow() is True  # first send
        assert allow() is False  # within the cooldown
        clock["now"] = T0 + timedelta(seconds=61)
        assert allow() is True  # cooldown elapsed
    finally:
        conn.close()


def test_the_daily_cap_holds_then_resets(tmp_path):
    clock = {"now": T0}
    conn = DurableConnection(tmp_path / "t.db")
    try:
        throttle = SendThrottle(conn, clock=lambda: clock["now"])
        sent = 0
        for i in range(10):
            clock["now"] = T0 + timedelta(minutes=11 * i)  # past the cooldown
            if throttle.allow("num", "sms", cooldown_s=60, per_day=3):
                sent += 1
        assert sent == 3  # the daily cap held
        # A new day reopens the door.
        clock["now"] = T0 + timedelta(days=1, minutes=1)
        assert throttle.allow("num", "sms", cooldown_s=60, per_day=3) is True
    finally:
        conn.close()


def test_identifiers_and_purposes_are_separate_ledgers(tmp_path):
    conn = DurableConnection(tmp_path / "t.db")
    try:
        throttle = SendThrottle(conn, clock=lambda: T0)
        assert throttle.allow("a", "reset", cooldown_s=60, per_day=1) is True
        # A different address, or the same address for a different purpose,
        # is a fresh ledger — one number's pacing never gates another's.
        assert throttle.allow("b", "reset", cooldown_s=60, per_day=1) is True
        assert throttle.allow("a", "sms", cooldown_s=60, per_day=1) is True
        assert throttle.allow("a", "reset", cooldown_s=60, per_day=1) is False
    finally:
        conn.close()
