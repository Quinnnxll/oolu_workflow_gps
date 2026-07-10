"""Regional currency: caps entered in the user's legal currency.

The meter counts USD; the user thinks in their region's money. The
currency module owns the fixed-reference-rate translation, the settings
catalog declares the currency choice and stamps money fields with the
resolved unit, and the chat router compares the converted cap and speaks
its refusal in the user's own currency.
"""

from __future__ import annotations

import pytest

from oolu.currency import (
    CURRENCIES,
    CURRENCY_CODES,
    format_amount,
    from_usd,
    to_usd,
)
from oolu.settings_node import SETTINGS_CATALOG, field_for


def test_conversion_round_trips_through_the_reference_rate():
    assert to_usd(155.0, "JPY") == pytest.approx(1.0)
    assert from_usd(2.0, "JPY") == pytest.approx(310.0)
    assert to_usd(10.0, "USD") == 10.0
    for code in CURRENCY_CODES:
        assert to_usd(from_usd(7.5, code), code) == pytest.approx(7.5)


def test_unknown_codes_read_as_usd_never_widening_a_cap():
    assert to_usd(5.0, "XXX") == 5.0
    assert to_usd(5.0, "") == 5.0
    assert format_amount(5.0, "nope").endswith("USD")


def test_format_speaks_the_regional_symbol_and_code():
    assert format_amount(1550, "JPY") == "¥1,550 JPY"
    assert format_amount(12.5, "EUR") == "€12.50 EUR"
    assert format_amount(86750, "MWK") == "MK 86,750.00 MWK"


def test_settings_declare_the_currency_choice_and_stamp_money_units():
    field = field_for("account.currency")
    assert field is not None and field.choices == CURRENCY_CODES
    assert field.default == "USD"
    money_keys = {
        f.key for f in SETTINGS_CATALOG if f.unit == "currency"
    }
    assert money_keys == {
        "budget.model_cap",
        "budget.hard_cap",
        "budget.review_threshold",
        "budget.monthly_limit",
    }


def test_describe_resolves_the_unit_to_the_tenants_currency(tmp_path):
    from oolu.durable import DurableConnection
    from oolu.settings_node import SettingsNode, SettingsStore

    conn = DurableConnection(tmp_path / "s.db")
    try:
        node = SettingsNode(SettingsStore(conn))
        by_key = {i["key"]: i for i in node.describe("t1")}
        assert by_key["budget.model_cap"]["unit"] == "USD"

        node.set("t1", "account.currency", "MWK")
        by_key = {i["key"]: i for i in node.describe("t1")}
        assert by_key["budget.model_cap"]["unit"] == "MWK"
        assert by_key["budget.hard_cap"]["unit"] == "MWK"
        # Non-money fields carry no unit.
        assert by_key["app.theme"]["unit"] is None

        with pytest.raises(Exception):
            node.set("t1", "account.currency", "DOGE")
    finally:
        conn.close()


def test_router_compares_the_cap_in_the_users_currency():
    from oolu.chat import ModelBudgetExceeded
    from oolu.providers.chatmodel import ChatModelRouter

    class _Meter:
        def __init__(self, spent_usd: float):
            self._spent = spent_usd

        def total_cost(self) -> float:
            return self._spent

    class _Keyring:
        def secret_for(self, tenant, provider):
            return None

        def providers(self, tenant):
            return []

    def router(spent_usd: float, cap: float, code: str) -> ChatModelRouter:
        return ChatModelRouter(
            _Keyring(),
            "t1",
            meter=_Meter(spent_usd),
            budget=lambda: cap,
            currency=lambda: code,
        )

    # A 1,550-yen cap is ten dollars: 9 spent passes, 11 refuses — and the
    # refusal speaks yen, not dollars.
    router(9.0, 1550.0, "JPY")._check_budget()
    with pytest.raises(ModelBudgetExceeded) as exc:
        router(11.0, 1550.0, "JPY").reply([{"role": "user", "content": "hi"}])
    message = str(exc.value)
    assert "¥1,550 JPY" in message
    assert "$" not in message.replace("¥", "")

    # USD behaves exactly as before.
    with pytest.raises(ModelBudgetExceeded):
        router(10.0, 10.0, "USD").reply([{"role": "user", "content": "hi"}])


def test_rates_are_sane_reference_data():
    for code, currency in CURRENCIES.items():
        assert currency.units_per_usd > 0, code
        assert currency.decimals in (0, 2), code
