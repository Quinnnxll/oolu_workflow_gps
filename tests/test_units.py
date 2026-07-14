"""The reply speaks the user's units — preference first, else region."""

from __future__ import annotations

from oolu.chat import (
    IMPERIAL_UNITS_NOTE,
    METRIC_UNITS_NOTE,
    region_from_locale,
    units_directive,
)
from oolu.settings_node import SETTINGS_CATALOG


def test_an_explicit_preference_wins_over_region():
    assert units_directive("metric", region="US") == METRIC_UNITS_NOTE
    assert units_directive("imperial", region="GB") == IMPERIAL_UNITS_NOTE


def test_auto_follows_the_region_with_si_as_the_default():
    # Only the three imperial holdouts get imperial; everyone else SI.
    assert units_directive("auto", region="US") == IMPERIAL_UNITS_NOTE
    assert units_directive("auto", region="LR") == IMPERIAL_UNITS_NOTE
    assert units_directive("auto", region="MM") == IMPERIAL_UNITS_NOTE
    assert units_directive("auto", region="GB") == METRIC_UNITS_NOTE
    assert units_directive("auto", region="KE") == METRIC_UNITS_NOTE


def test_auto_without_a_region_is_si():
    assert units_directive("auto", region=None) == METRIC_UNITS_NOTE
    assert units_directive(None, region=None) == METRIC_UNITS_NOTE
    assert units_directive("", region=None) == METRIC_UNITS_NOTE


def test_region_is_read_from_the_browser_accept_language():
    assert region_from_locale("en-US,en;q=0.9") == "US"
    assert region_from_locale("fr-FR") == "FR"
    assert region_from_locale("en_GB") == "GB"
    assert region_from_locale("zh-Hant-TW") == "TW"
    # A bare language (no region) or nothing yields no region.
    assert region_from_locale("en") is None
    assert region_from_locale("zh-Hant") is None
    assert region_from_locale(None) is None
    assert region_from_locale("") is None


def test_units_is_a_declared_account_setting():
    field = next(f for f in SETTINGS_CATALOG if f.key == "account.units")
    assert field.group == "account"
    assert field.default == "auto"
    assert set(field.choices) == {"auto", "metric", "imperial"}
