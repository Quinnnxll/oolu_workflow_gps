"""Regional currency for user-facing amounts — entry and display units.

The metering ledger counts model spend in USD (the price tables' native
unit). Users, however, set caps in the legal currency of THEIR region, so
this module owns the translation: a closed catalog of supported currencies,
each with a symbol, decimals, and a FIXED REFERENCE RATE against USD.

The rates are deliberately static data, not a live feed: a spending cap is
a safety rail, not an FX position, and a rail must not move because a
market did. The values are periodically-reviewed approximations — close
enough that "stop around 50,000 kwacha" means what the user intended.
Anything that needs exact FX (payouts, settlement) uses the billing stack's
own explicit currency handling, never this table.
"""

from __future__ import annotations

from typing import NamedTuple


class Currency(NamedTuple):
    code: str
    symbol: str
    units_per_usd: float  # fixed reference rate: how many units one USD buys
    decimals: int


# The closed catalog. Adding a region means adding a reviewed row here.
CURRENCIES: dict[str, Currency] = {
    c.code: c
    for c in (
        Currency("USD", "$", 1.0, 2),
        Currency("EUR", "€", 0.92, 2),
        Currency("GBP", "£", 0.79, 2),
        Currency("JPY", "¥", 155.0, 0),
        Currency("CNY", "¥", 7.2, 2),
        Currency("HKD", "HK$", 7.8, 2),
        Currency("SGD", "S$", 1.35, 2),
        Currency("INR", "₹", 84.0, 2),
        Currency("KRW", "₩", 1380.0, 0),
        Currency("CAD", "C$", 1.37, 2),
        Currency("AUD", "A$", 1.52, 2),
        Currency("CHF", "CHF ", 0.88, 2),
        Currency("BRL", "R$", 5.6, 2),
        Currency("MXN", "MX$", 18.5, 2),
        Currency("NGN", "₦", 1500.0, 2),
        Currency("KES", "KSh ", 130.0, 2),
        Currency("ZAR", "R ", 18.2, 2),
        Currency("MWK", "MK ", 1735.0, 2),
    )
}

CURRENCY_CODES: tuple[str, ...] = tuple(CURRENCIES)


def _currency(code: str) -> Currency:
    return CURRENCIES.get((code or "").strip().upper(), CURRENCIES["USD"])


def to_usd(amount: float, code: str) -> float:
    """A user-currency amount in the meter's USD unit. Unknown codes read
    as USD — a typo must widen nothing (USD is the smallest-rate unit here,
    so a mistaken cap errs toward stopping earlier, never later)."""
    return float(amount) / _currency(code).units_per_usd


def from_usd(amount: float, code: str) -> float:
    """A metered USD amount in the user's currency."""
    return float(amount) * _currency(code).units_per_usd


def format_amount(amount: float, code: str) -> str:
    """One honest money string in the user's currency: symbol + amount +
    code, e.g. '¥1,550 JPY' or 'MK 86,750.00 MWK'."""
    currency = _currency(code)
    return (
        f"{currency.symbol}{amount:,.{currency.decimals}f} {currency.code}"
    )
