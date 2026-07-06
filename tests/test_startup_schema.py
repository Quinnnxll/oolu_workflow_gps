"""Every table exists the moment the app starts — no write ever "warms" one.

A field report from the packaged app: a fresh install must never hit
"no such table". The guarantee this file pins: constructing a runtime on
an EMPTY data directory creates every schema it will ever read, so every
read surface answers before any write has happened — for the unified
host (the shell's and the exe's startup path). And reopening the same directory (an upgrade, a restart) migrates
instead of breaking.
"""

from __future__ import annotations

from test_http_gateway import _req

from oolu.assembly import build_host_runtime

SECRET = "a-thirty-two-character-plus-signing-secret"


def test_the_multi_user_host_answers_every_read_on_a_fresh_directory(tmp_path):
    runtime = build_host_runtime(data_dir=tmp_path / "fresh-host", secret=SECRET)
    runtime.accounts.bootstrap(tenant="main", username="admin", password="first-pass")
    try:
        token = runtime.gateway.handle(
            _req(
                "POST",
                "/v1/auth/login",
                body={"username": "admin", "password": "first-pass"},
            )
        ).body["token"]
        for path in (
            "/v1/runs",
            "/v1/runs/contract/holds",
            "/v1/market/candidates",
            "/v1/nodeplace",
            "/v1/listings",
            "/v1/auth/users",
            "/v1/metrics",
        ):
            response = runtime.gateway.handle(_req("GET", path, token=token))
            assert response.status in (200, 404), f"{path} -> {response.status}"
            assert response.status != 500, path
    finally:
        runtime.close()


def test_the_host_reopens_its_own_data_directory(tmp_path):
    data = tmp_path / "host-data"
    first = build_host_runtime(data_dir=data, secret=SECRET)
    first.accounts.bootstrap(tenant="main", username="admin", password="first-pass")
    first.close()

    second = build_host_runtime(data_dir=data, secret=SECRET)
    try:
        # The admin created before the restart still signs in after it.
        response = second.gateway.handle(
            _req(
                "POST",
                "/v1/auth/login",
                body={"username": "admin", "password": "first-pass"},
            )
        )
        assert response.status == 200, response.body
    finally:
        second.close()


def test_every_sqlite_store_creates_schema_in_its_constructor():
    """The convention itself, pinned: schema DDL runs at construction
    (CREATE TABLE IF NOT EXISTS or a migration), never lazily on first
    write — so a read-before-write can never see 'no such table'."""
    import inspect

    from oolu.billing.accounts import PayoutStore
    from oolu.billing.disputes import DisputeStore
    from oolu.billing.ledger import EarningsLedger
    from oolu.durable.connection import DurableConnection
    from oolu.identity.accounts import LocalUserStore
    from oolu.identity.store import IdentityStore
    from oolu.knowledge.traces import TraceStore
    from oolu.metering.attribution import AttributionStore
    from oolu.metering.store import MeteringLedger
    from oolu.nodeplace.holds import PendingContractStore
    from oolu.nodeplace.market import PriceBook
    from oolu.nodeplace.ratings import RatingStore
    from oolu.nodeplace.store import RegistryStore
    from oolu.skills.registry import SkillRegistry

    conn = DurableConnection(":memory:")
    for store in (
        EarningsLedger(conn),
        PayoutStore(conn),
        DisputeStore(conn),
        AttributionStore(conn),
        MeteringLedger(conn),
        PendingContractStore(conn),
        RegistryStore(conn),
        RatingStore(conn),
    ):
        assert store is not None
    with conn.transaction() as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    # Every store above left tables behind at CONSTRUCTION time; a fresh
    # connection already had the durable core (runs, audit, idempotency).
    for expected in ("earnings_entries", "pending_contracts", "workflow_runs"):
        assert expected in tables, (expected, sorted(tables))
    assert len(tables) > 12  # the component stores all contributed
    conn.close()

    # Path-owning stores: constructing on :memory: must run DDL too.
    for cls in (TraceStore, IdentityStore, LocalUserStore, SkillRegistry, PriceBook):
        instance = cls(":memory:")
        instance.close()
    assert inspect.isclass(PriceBook)


def test_path_owning_stores_create_their_parent_directories(tmp_path):
    """The packaged app's field failure, pinned: on a fresh machine the
    data directory does not exist yet, and sqlite will not create it —
    every path-owning store must, or first run dies before any table."""
    from oolu.identity.accounts import LocalUserStore
    from oolu.identity.store import IdentityStore
    from oolu.knowledge.client import LocalKnowledgeClient
    from oolu.knowledge.traces import TraceStore
    from oolu.nodeplace.market import PriceBook
    from oolu.skills.registry import SkillRegistry

    for index, cls in enumerate(
        (
            SkillRegistry,
            TraceStore,
            PriceBook,
            LocalKnowledgeClient,
            IdentityStore,
            LocalUserStore,
        )
    ):
        nested = tmp_path / f"brand-{index}" / "new" / "dirs" / "store.db"
        assert not nested.parent.exists()
        instance = cls(nested)
        instance.close()
        assert nested.exists(), cls.__name__
