from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..identity.errors import IdentityConfigurationError
from ..identity.tokens import ProviderConfig, assert_production_identity


class MoneyModeError(RuntimeError):
    pass


def require_production_money(durable: Any, providers: Iterable[ProviderConfig]) -> None:
    if not getattr(durable, "is_production_durable", False):
        raise MoneyModeError(
            "real money requires the production PostgreSQL durable adapter; "
            "charging and payout are refused on the local durable adapter"
        )
    try:
        assert_production_identity(providers)
    except IdentityConfigurationError as exc:
        raise MoneyModeError(str(exc)) from exc


def is_production_money(durable: Any, providers: Iterable[ProviderConfig]) -> bool:
    try:
        require_production_money(durable, providers)
    except MoneyModeError:
        return False
    return True
