"""The production dialect: the marketplace boots and settles on PostgreSQL.

The 502 postmortem, as tests. The marketplace stores were written in the
SQLite idiom and are constructed inside ``GatewayApp.__init__`` — so the
first deploy onto the production PostgreSQL connection crashed the gateway
at CREATE TABLE (``AUTOINCREMENT`` is SQLite-only), Caddy answered 502,
and Cloudflare relayed it. Three dialect drifts, three pins:

- the translator rewrites ``INTEGER PRIMARY KEY AUTOINCREMENT`` to
  ``BIGSERIAL`` (and now refuses an untranslatable REPLACE loudly);
- the policy stores upsert portably;
- invoice numbering reads its sequence back instead of trusting a
  ``lastrowid`` PostgreSQL never returns.

The pure translator tests run everywhere; the server-backed tests build
THE GATEWAY ITSELF over ``PostgresDurableConnection`` — the exact boot
that failed — then walk an escrow order to a balanced book and a
correctly numbered invoice, twice over the same tables to prove the
second boot finds the schema it left.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from test_http_gateway import _autonomous, _factory, _Identity, _req
from test_marketplace_spine import _SAFE_RISK, _delegation, _offer

from oolu.billing.doubleentry import DoubleEntryLedger
from oolu.billing.escrow import EscrowPolicy
from oolu.billing.psp import FakePsp
from oolu.billing.tax import InvoiceBook, JurisdictionModule
from oolu.durable.postgres import PostgresDurableConnection, _translate
from oolu.durable.service import DurableWorkflowService
from oolu.gateway import GatewayApp
from oolu.marketplace import (
    MarketplaceSpine,
    OrderService,
    PurchasePolicy,
    SalesPolicy,
    SalesPolicyStore,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
M = 1_000_000
PG_DSN = os.environ.get("OOLU_TEST_PG_DSN") or os.environ.get("DATABASE_URL")

_MARKET_TABLES = (
    "commercial_intents",
    "market_approvals",
    "market_delegations",
    "market_imported_offers",
    "market_jobs",
    "market_listings",
    "market_milestones",
    "market_negotiations",
    "market_orders",
    "market_payout_changes",
    "market_peers",
    "market_policies",
    "market_quotes",
    "market_reconciliations",
    "market_recurring",
    "market_reservations",
    "market_rfqs",
    "market_sales_policies",
    "market_stock",
    "marketplace_invoices",
    "marketplace_ledger",
    "kyc_records",
)


# --------------------------------------------------------------------------- #
# The translator, pure — these pins run on every machine.                      #
# --------------------------------------------------------------------------- #
def test_autoincrement_translates_to_bigserial():
    translated = _translate(
        "CREATE TABLE IF NOT EXISTS marketplace_ledger ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT, txn_id TEXT NOT NULL)"
    )
    assert "AUTOINCREMENT" not in translated
    assert "BIGSERIAL PRIMARY KEY" in translated


def test_replace_translates_composite_keys_and_refuses_unknown_tables():
    translated = _translate(
        "INSERT OR REPLACE INTO trace_node_stats"
        " (node_key, context, successes) VALUES (?, ?, ?)"
    )
    assert "ON CONFLICT (node_key, context) DO UPDATE" in translated
    assert "successes = EXCLUDED.successes" in translated
    with pytest.raises(NotImplementedError, match="mystery_table"):
        _translate("INSERT OR REPLACE INTO mystery_table (a) VALUES (?)")


# --------------------------------------------------------------------------- #
# The server-backed pins.                                                      #
# --------------------------------------------------------------------------- #
def _pg() -> PostgresDurableConnection:
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        for table in _MARKET_TABLES:
            db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    return conn


def test_the_gateway_boots_on_postgres(tmp_path):
    """The exact construction the production container runs — the one
    that 502'd. Twice, over the same schema, because the second deploy
    must find the tables the first one left."""
    conn = _pg()
    ident = _Identity(tmp_path)
    brief, blueprint, executor, grounding = _autonomous()
    for boot in (1, 2):
        durable = DurableWorkflowService(
            conn, _factory(brief, blueprint, executor, grounding)
        )
        app = GatewayApp(
            durable,
            validator=ident.validator,
            resolver=ident.resolver,
            approval_authority=ident.authority,
        )
        assert app.handle(_req("GET", "/v1/openapi.json")).status == 200, boot
    conn.close()


def test_the_money_flow_settles_and_numbers_invoices_on_postgres(tmp_path):
    conn = _pg()
    from oolu.durable.audit import DurableAuditLog

    audit = DurableAuditLog(conn)
    spine = MarketplaceSpine(conn, audit=audit)
    ledger = DoubleEntryLedger(conn)
    invoices = InvoiceBook(conn)
    orders = OrderService(
        conn,
        audit=audit,
        spine=spine,
        psp=FakePsp(),
        ledger=ledger,
        take_rate_bps=500,
        escrow=EscrowPolicy(),
        invoices=invoices,
        jurisdiction=JurisdictionModule(code="US", tax_rate_bps=0, invoice_prefix="US"),
    )
    spine.grant_delegation(_delegation())

    # The policy upsert, both branches: insert, then update in place.
    spine.set_purchase_policy(
        PurchasePolicy(auto_purchase_limit_micros=10 * M),
        tenant="t1",
        principal="user-1",
    )
    spine.set_purchase_policy(
        PurchasePolicy(auto_purchase_limit_micros=20 * M),
        tenant="t1",
        principal="user-1",
    )
    assert (
        spine.purchase_policy(tenant="t1", principal="user-1")
        .auto_purchase_limit_micros
        == 20 * M
    )
    sales = SalesPolicyStore(conn)
    sales.put(SalesPolicy(absolute_floor_micros=5 * M), tenant="t1", principal="s")
    sales.put(SalesPolicy(absolute_floor_micros=7 * M), tenant="t1", principal="s")
    assert (
        sales.get(tenant="t1", principal="s").absolute_floor_micros == 7 * M
    )

    stored, _ = spine.create_purchase_intent(
        tenant="t1",
        principal="user-1",
        agent="oolu",
        offer=_offer(tax_estimate_micros=0),
        idempotency_key="pg-1",
        now=NOW,
        category="household",
        delivery_destination="home",
        risk_facts=dict(_SAFE_RISK),
    )
    spine.record_approval(
        stored.intent.intent_id,
        tenant="t1",
        approver_id="user-1",
        assurance_level=1,
        approve=True,
        now=NOW,
    )
    order, created = orders.place_order(
        intent_id=stored.intent.intent_id,
        tenant="t1",
        now=NOW,
        customer_ref="cus_1",
        payment_method_ref="pm_ok",
    )
    assert created
    order_id = order.record.order_id
    orders.mark_shipped(order_id, tenant="t1", actor="user-1", now=NOW)
    orders.mark_delivered(
        order_id, tenant="t1", actor="user-1", now=NOW, evidence="photo"
    )
    orders.accept(order_id, tenant="t1", actor="user-1", now=NOW)
    assert ledger.take_micros() == 5 * M
    assert sum(ledger.trial_balance().values()) == 0
    invoice = invoices.for_order(order_id)
    assert invoice is not None
    assert invoice.number == "US-000001"  # the sequence, not lastrowid's None
    kinds = [t.kind for t in ledger.transactions(order_id=order_id)]
    assert kinds == ["capture", "release"]  # explicit posting order holds
    orders.refund(order_id, tenant="t1", actor="user-1", now=NOW, reason="test")
    assert all(v == 0 for v in ledger.trial_balance().values())
    conn.close()
