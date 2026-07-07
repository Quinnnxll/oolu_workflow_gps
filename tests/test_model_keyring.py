"""The model keyring: pasted keys survive restarts, encrypted at rest.

The guarantees under test are the ones that matter for a real machine:
the database file alone never contains a usable key, a wrong machine key
or a tampered row fails closed, and the read surface exposes fingerprints
only — the secret leaves through ``secret_for`` alone.
"""

from __future__ import annotations

import pytest

from oolu.durable.connection import DurableConnection
from oolu.providers.keyring import KeyringError, ModelKeyring, fingerprint

SECRET = "sk-ant-test-1234567890abcdef"


def _keyring(tmp_path, name="durable.db", key_name="machine.key"):
    conn = DurableConnection(tmp_path / name)
    return ModelKeyring(conn, key_path=tmp_path / key_name), conn


def test_store_and_mint_roundtrip(tmp_path):
    keyring, conn = _keyring(tmp_path)
    mark = keyring.store("t1", "anthropic", SECRET)
    assert mark == fingerprint(SECRET)
    assert keyring.secret_for("t1", "anthropic") == SECRET
    # Tenant-scoped: another tenant sees nothing.
    assert keyring.secret_for("t2", "anthropic") is None
    conn.close()


def test_keys_survive_a_restart(tmp_path):
    keyring, conn = _keyring(tmp_path)
    keyring.store("t1", "openai", SECRET)
    conn.close()

    reopened, conn = _keyring(tmp_path)  # same db, same machine key file
    assert reopened.secret_for("t1", "openai") == SECRET
    conn.close()


def test_the_database_alone_never_contains_the_key(tmp_path):
    keyring, conn = _keyring(tmp_path)
    keyring.store("t1", "anthropic", SECRET)
    with conn.lock:
        row = conn.db.execute(
            "SELECT ciphertext, fingerprint FROM model_keys"
        ).fetchone()
    assert SECRET not in row["ciphertext"]
    assert SECRET.encode().hex() not in row["ciphertext"]
    assert SECRET not in row["fingerprint"]
    # And the listing surface is fingerprints only.
    listing = keyring.providers("t1")
    assert listing[0]["fingerprint"] == fingerprint(SECRET)
    assert all(SECRET not in str(v) for item in listing for v in item.values())
    conn.close()


def test_wrong_machine_key_fails_closed(tmp_path):
    keyring, conn = _keyring(tmp_path)
    keyring.store("t1", "anthropic", SECRET)
    conn.close()

    # Same database, different machine key: authentication must fail —
    # never silently decrypt to garbage that gets sent as an auth header.
    other, conn = _keyring(tmp_path, key_name="other.key")
    with pytest.raises(KeyringError):
        other.secret_for("t1", "anthropic")
    conn.close()


def test_tampered_ciphertext_fails_closed(tmp_path):
    keyring, conn = _keyring(tmp_path)
    keyring.store("t1", "anthropic", SECRET)
    with conn.transaction() as db:
        row = db.execute("SELECT ciphertext FROM model_keys").fetchone()
        flipped = ("0" if row["ciphertext"][0] != "0" else "1") + row["ciphertext"][1:]
        db.execute("UPDATE model_keys SET ciphertext = ?", (flipped,))
    with pytest.raises(KeyringError):
        keyring.secret_for("t1", "anthropic")
    conn.close()


def test_replace_and_remove(tmp_path):
    keyring, conn = _keyring(tmp_path)
    keyring.store("t1", "anthropic", SECRET)
    keyring.store("t1", "anthropic", "sk-ant-replacement-key")
    assert keyring.secret_for("t1", "anthropic") == "sk-ant-replacement-key"
    assert len(keyring.providers("t1")) == 1

    assert keyring.remove("t1", "anthropic") is True
    assert keyring.remove("t1", "anthropic") is False
    assert keyring.secret_for("t1", "anthropic") is None
    conn.close()


def test_refuses_junk(tmp_path):
    keyring, conn = _keyring(tmp_path)
    with pytest.raises(ValueError):
        keyring.store("t1", "not-a-provider", SECRET)
    with pytest.raises(ValueError):
        keyring.store("t1", "anthropic", "short")
    conn.close()


def test_machine_key_file_is_private(tmp_path):
    import os
    import stat

    _, conn = _keyring(tmp_path)
    mode = stat.S_IMODE(os.stat(tmp_path / "machine.key").st_mode)
    assert mode == 0o600
    conn.close()
