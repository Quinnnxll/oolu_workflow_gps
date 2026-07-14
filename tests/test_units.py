"""The reply speaks the user's units — preference first, else region."""

from __future__ import annotations

from oolu.chat import (
    IMPERIAL_UNITS_NOTE,
    METRIC_UNITS_NOTE,
    units_directive,
)
from oolu.settings_node import SETTINGS_CATALOG


def test_an_explicit_preference_wins_over_currency():
    assert units_directive("metric", currency="USD") == METRIC_UNITS_NOTE
    assert units_directive("imperial", currency="GBP") == IMPERIAL_UNITS_NOTE


def test_auto_follows_the_spending_currency_with_si_as_the_default():
    # Only the imperial holdouts' currencies get imperial; everyone else SI.
    assert units_directive("auto", currency="USD") == IMPERIAL_UNITS_NOTE
    assert units_directive("auto", currency="LRD") == IMPERIAL_UNITS_NOTE
    assert units_directive("auto", currency="MMK") == IMPERIAL_UNITS_NOTE
    assert units_directive("auto", currency="GBP") == METRIC_UNITS_NOTE
    assert units_directive("auto", currency="KES") == METRIC_UNITS_NOTE
    assert units_directive("auto", currency="usd") == IMPERIAL_UNITS_NOTE  # case


def test_auto_without_a_currency_is_si():
    assert units_directive("auto", currency=None) == METRIC_UNITS_NOTE
    assert units_directive(None, currency=None) == METRIC_UNITS_NOTE
    assert units_directive("", currency=None) == METRIC_UNITS_NOTE


def test_units_is_a_declared_account_setting():
    field = next(f for f in SETTINGS_CATALOG if f.key == "account.units")
    assert field.group == "account"
    assert field.default == "auto"
    assert set(field.choices) == {"auto", "metric", "imperial"}
