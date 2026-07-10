"""The settings node — configuring OoLu through a declared, bounded catalog.

The property under test is the guarantee: configuration changes go through
``SettingsNode.set`` against ``SETTINGS_CATALOG`` and nowhere else. Unknown
keys and out-of-bounds values are refused; there is no code path that lets a
caller (or the assistant) invent a knob or smuggle a value.
"""

from __future__ import annotations

import pytest

from oolu.settings_node import (
    SETTINGS_CATALOG,
    SettingError,
    SettingsNode,
    SettingsStore,
    field_for,
)


def _node(tmp_path):
    from oolu.durable import DurableConnection

    conn = DurableConnection(tmp_path / "d.db")
    return SettingsNode(SettingsStore(conn)), conn


def test_effective_starts_at_declared_defaults(tmp_path):
    node, conn = _node(tmp_path)
    try:
        values = node.effective("t1")
        assert values["app.theme"] == "system"
        assert values["subscription.plan"] == "free"
        assert values["budget.hard_cap"] == 0.0
        # Every catalog field has a value; nothing is missing or invented.
        assert set(values) == {f.key for f in SETTINGS_CATALOG}
    finally:
        conn.close()


def test_set_within_bounds_persists_and_is_tenant_scoped(tmp_path):
    node, conn = _node(tmp_path)
    try:
        node.set("t1", "budget.hard_cap", 25)
        node.set("t1", "app.theme", "dark")
        assert node.effective("t1")["budget.hard_cap"] == 25.0
        assert node.effective("t1")["app.theme"] == "dark"
        # A different tenant is untouched.
        assert node.effective("t2")["budget.hard_cap"] == 0.0
    finally:
        conn.close()


def test_unknown_key_is_refused(tmp_path):
    node, conn = _node(tmp_path)
    try:
        with pytest.raises(SettingError, match="no such setting"):
            node.set("t1", "app.secret_backdoor", "please")
        # Nothing was written.
        assert "app.secret_backdoor" not in node.effective("t1")
    finally:
        conn.close()


def test_out_of_bounds_number_is_refused(tmp_path):
    node, conn = _node(tmp_path)
    try:
        with pytest.raises(SettingError, match="at most"):
            node.set("t1", "budget.hard_cap", 1_000_000_000)
        with pytest.raises(SettingError, match="at least"):
            node.set("t1", "budget.hard_cap", -5)
        # The refused writes left the default intact.
        assert node.effective("t1")["budget.hard_cap"] == 0.0
    finally:
        conn.close()


def test_choice_outside_the_closed_set_is_refused(tmp_path):
    node, conn = _node(tmp_path)
    try:
        with pytest.raises(SettingError, match="one of"):
            node.set("t1", "model.provider", "skynet")  # not offered
        with pytest.raises(SettingError, match="one of"):
            node.set("t1", "app.theme", "neon")
        # subscription.plan refuses even in-set values: it is managed —
        # the account console's cancel-first flow owns it, not a knob.
        with pytest.raises(SettingError, match="account console"):
            node.set("t1", "subscription.plan", "pro")
    finally:
        conn.close()


def test_bool_and_text_coercion(tmp_path):
    node, conn = _node(tmp_path)
    try:
        assert node.set("t1", "app.notifications", "off") is False
        assert node.set("t1", "app.notifications", "yes") is True
        with pytest.raises(SettingError):
            node.set("t1", "account.display_name", "x" * 200)  # over max_length
    finally:
        conn.close()


def test_set_many_is_all_or_nothing(tmp_path):
    node, conn = _node(tmp_path)
    try:
        with pytest.raises(SettingError):
            node.set_many(
                "t1",
                {"app.theme": "light", "budget.hard_cap": "not-a-number"},
            )
        # The valid change in the batch did NOT commit — the bad one aborted it.
        assert node.effective("t1")["app.theme"] == "system"
    finally:
        conn.close()


def test_catalog_describes_bounds_for_the_assistant(tmp_path):
    node, conn = _node(tmp_path)
    try:
        described = {item["key"]: item for item in node.describe("t1")}
        plan = described["subscription.plan"]
        assert plan["kind"] == "choice"
        assert plan["choices"] == ["free", "plus", "pro", "enterprise"]
        assert plan["value"] == "free"
        cap = described["budget.hard_cap"]
        # High-rate currencies (JPY, KRW, MWK) need headroom: the bound is
        # wide because the unit is the user's regional currency, not USD.
        assert cap["maximum"] == 100_000_000.0
        assert cap["unit"] == "USD"  # resolved from account.currency
    finally:
        conn.close()


def test_the_node_has_no_executable_body():
    """Structural guarantee: settings are data + bounds, never code.

    A field carries a type and bounds — never a script, command, or
    callable. The assistant cannot 'rewrite the code' because there is no
    code on the node to rewrite; there is only the coerce-against-bounds
    door.
    """
    for field in SETTINGS_CATALOG:
        dumped = field.model_dump()
        assert "body" not in dumped and "script" not in dumped
        assert "command" not in dumped and "actions" not in dumped
        assert field_for(field.key) is field
