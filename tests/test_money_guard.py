from __future__ import annotations

import os

import pytest

from workflow_gps.billing import (
    MoneyModeError,
    is_production_money,
    require_production_money,
)
from workflow_gps.durable import DurableConnection
from workflow_gps.durable.postgres import PostgresDurableConnection
from workflow_gps.identity import ProviderConfig
from workflow_gps.identity.tokens import Hs256Verifier

PG_DSN = os.environ.get("WFGPS_TEST_PG_DSN") or os.environ.get("DATABASE_URL")


class _AsymmetricVerifier:
    algorithm = "RS256"

    def verify(self, signing_input, signature, header) -> bool:
        return True


class _ProductionDurable:
    is_production_durable = True


def _provider(verifier):
    return ProviderConfig(
        issuer="https://idp", audiences=frozenset({"wfgps"}), verifier=verifier
    )


_ASYMMETRIC = [_provider(_AsymmetricVerifier())]
_SYMMETRIC = [_provider(Hs256Verifier("secret"))]


def test_local_durable_refuses_money():
    conn = DurableConnection(":memory:")
    try:
        with pytest.raises(MoneyModeError):
            require_production_money(conn, _ASYMMETRIC)
        assert is_production_money(conn, _ASYMMETRIC) is False
    finally:
        conn.close()


def test_symmetric_identity_refuses_money():
    with pytest.raises(MoneyModeError):
        require_production_money(_ProductionDurable(), _SYMMETRIC)
    assert is_production_money(_ProductionDurable(), _SYMMETRIC) is False


def test_production_durable_and_asymmetric_identity_permit_money():
    assert is_production_money(_ProductionDurable(), _ASYMMETRIC) is True
    require_production_money(_ProductionDurable(), _ASYMMETRIC)


def test_local_durable_class_marker_is_false():
    assert DurableConnection.is_production_durable is False
    assert PostgresDurableConnection.is_production_durable is True


@pytest.mark.needs_postgres
def test_real_postgres_connection_permits_money():
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    conn = PostgresDurableConnection(PG_DSN)
    try:
        require_production_money(conn, _ASYMMETRIC)
        with pytest.raises(MoneyModeError):
            require_production_money(conn, _SYMMETRIC)
    finally:
        conn.close()
