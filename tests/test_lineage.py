"""Lineage records: derivation provenance -> automatic royalty ancestors."""

from __future__ import annotations

import pytest
from test_gateway_market import _build, _contribute_and_publish, _req, _seed

from oolu.billing import BillingService, EarningsLedger
from oolu.metering.deriver import MeteringDeriver
from oolu.nodeplace import ContributionError, NodeplaceService, RegistryStore
from oolu.skills.models import ActionEvent, ReusableSkill, SkillSignature


def _skill(name: str, operation: str = "run") -> ReusableSkill:
    return ReusableSkill(
        name=name,
        description=f"{name} does work",
        signature=SkillSignature(application="cli", adapter="cli"),
        actions=[ActionEvent(correlation_id="c", adapter="cli", operation=operation)],
    )


class _Conn:
    """Minimal durable-connection double for a bare RegistryStore."""

    def __init__(self, tmp_path):
        import sqlite3
        import threading
        from contextlib import contextmanager

        self.db = sqlite3.connect(tmp_path / "reg.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()

        @contextmanager
        def transaction():
            with self.lock:
                yield self.db
                self.db.commit()

        self.transaction = transaction


def test_lineage_chains_shift_levels_and_cap(tmp_path):
    service = NodeplaceService(RegistryStore(_Conn(tmp_path)))

    def contribute(name, noder, derived_from=None):
        return service.contribute(
            noder_principal=noder,
            tenant_id="t1",
            skill=_skill(name),
            semver="1.0.0",
            title=name,
            summary=name,
            derived_from=derived_from,
        )

    root = contribute("root", "noder-root")
    assert root.version.lineage == []

    child = contribute("child", "noder-child", derived_from=root.version.version_id)
    (record,) = child.version.lineage
    assert record.ancestor_noder_principal == "noder-root" and record.level == 1

    grand = contribute("grand", "noder-grand", derived_from=child.version.version_id)
    by_level = {r.level: r.ancestor_noder_principal for r in grand.version.lineage}
    assert by_level == {1: "noder-child", 2: "noder-root"}

    # Depth caps: a sixth-generation descendant drops ancestors beyond 5.
    current = grand
    noders = ["n3", "n4", "n5", "n6"]
    for noder in noders:
        current = contribute(
            f"gen-{noder}", noder, derived_from=current.version.version_id
        )
    levels = [r.level for r in current.version.lineage]
    assert max(levels) == 5 and len(levels) == 5

    with pytest.raises(ContributionError, match="unknown version"):
        contribute("orphan", "noder-x", derived_from="no-such-version")


def test_derived_run_pays_royalties_upstream_automatically(tmp_path):
    """contribute(derived_from) -> run -> verified success -> both noders paid."""
    app, conn, ident, registry, metering, attribution, audit = _build(tmp_path)
    proven, _flaky = _seed(app, ident, registry, metering, attribution, audit)

    derived = _contribute_and_publish(
        app,
        ident,
        registry,
        name="derived cleaner",
        noder="noder-deriver",
        price=0.30,
        derived_from=proven,
    )

    resp = app.handle(
        _req(
            "POST",
            "/v1/runs",
            token=ident.token("consumer", "t2"),
            body={"intent": "clean invoices", "node_version_id": derived},
        )
    )
    assert resp.status == 202, resp.body
    # The royalty ancestor was filled from provenance, not from the caller.
    assert set(resp.body["market"]["noders"]) == {"noder-deriver", "noder-good"}

    run_id = resp.body["run_id"]
    binding = attribution.get_binding(run_id)
    weights = {s.noder_principal: s.weight for s in binding.shares}
    assert weights["noder-deriver"] > weights["noder-good"] > 0
    assert weights["noder-good"] / weights["noder-deriver"] == pytest.approx(0.35)

    # Verified success -> the split pays both, conserving to the micro.
    events = MeteringDeriver(audit, metering, attribution).derive()
    event = next(e for e in events if e.run_id == run_id)
    billing = BillingService(EarningsLedger(conn))
    result = billing.price(event, attribution.attributions(event.event_id))
    assert result.conserves()
    assert result.noder_micros["noder-deriver"] > result.noder_micros["noder-good"] > 0
    conn.close()
