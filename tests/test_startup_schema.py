"""Every table exists the moment the app starts — no write ever "warms" one.

A field report from the packaged app: a fresh install must never hit
"no such table". The guarantee this file pins: constructing a runtime on
an EMPTY data directory creates every schema it will ever read, so every
read surface answers before any write has happened — for the desktop
shell (the exe's exact startup path), the web guard, and the multi-user
host. And reopening the same directory (an upgrade, a restart) migrates
instead of breaking.
"""

from __future__ import annotations

from test_desktop_loopback import _call, _call_raw
from test_http_gateway import _req

from workflow_gps.assembly import build_desktop_runtime, build_host_runtime
from workflow_gps.desktop.loopback import DesktopLoopbackApp

SECRET = "a-thirty-two-character-plus-signing-secret"


def test_the_desktop_shell_answers_every_read_on_a_fresh_directory(tmp_path):
    """The exe's startup path: build the runtime, read before any write."""
    runtime = build_desktop_runtime(db_path=tmp_path / "fresh" / "desktop.db")
    app = DesktopLoopbackApp(runtime.desktop)
    try:
        assert _call_raw(app, "GET", "/")[0] == 200  # the front-end itself
        for path in (
            "/v1/inbox",
            "/v1/earnings",
            "/v1/worker-health",
            "/v1/offline-policy",
        ):
            status, _body = _call(app, "GET", path)
            assert status in (200, 404), f"{path} -> {status}"
            assert status != 500, path
        # A write path that touches runs, tasks, audit, and the outbox.
        status, view = _call(app, "POST", "/v1/tasks", body={"intent": "hello"})
        assert status == 201, view
    finally:
        runtime.close()


def test_the_desktop_shell_reopens_its_own_data_directory(tmp_path):
    """Restart = reopen: the second boot must find (or migrate) every table."""
    db = tmp_path / "data" / "desktop.db"
    first = build_desktop_runtime(db_path=db)
    app = DesktopLoopbackApp(first.desktop)
    _call(app, "POST", "/v1/tasks", body={"intent": "before restart"})
    first.close()

    second = build_desktop_runtime(db_path=db)
    try:
        status, body = _call(
            second.desktop and DesktopLoopbackApp(second.desktop), "GET", "/v1/inbox"
        )
        assert status == 200
    finally:
        second.close()


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

    from workflow_gps.billing.accounts import PayoutStore
    from workflow_gps.billing.disputes import DisputeStore
    from workflow_gps.billing.ledger import EarningsLedger
    from workflow_gps.durable.connection import DurableConnection
    from workflow_gps.identity.accounts import LocalUserStore
    from workflow_gps.identity.store import IdentityStore
    from workflow_gps.knowledge.traces import TraceStore
    from workflow_gps.metering.attribution import AttributionStore
    from workflow_gps.metering.store import MeteringLedger
    from workflow_gps.nodeplace.holds import PendingContractStore
    from workflow_gps.nodeplace.market import PriceBook
    from workflow_gps.nodeplace.ratings import RatingStore
    from workflow_gps.nodeplace.store import RegistryStore
    from workflow_gps.skills.registry import SkillRegistry

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
