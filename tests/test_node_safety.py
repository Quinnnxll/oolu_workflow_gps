from __future__ import annotations

import pytest

from workflow_gps.durable import DurableConnection
from workflow_gps.nodeplace import (
    NodeplaceService,
    NodeSafetyGate,
    RegistryStore,
    SafetyViolation,
    Visibility,
)
from workflow_gps.skills.models import ActionEvent, ReusableSkill, SkillSignature


def _skill(*, operation="run"):
    return ReusableSkill(
        name="node",
        description="a node",
        signature=SkillSignature(application="shell", adapter="cli"),
        actions=[ActionEvent(correlation_id="c1", adapter="cli", operation=operation)],
    )


def _service():
    return NodeplaceService(RegistryStore(DurableConnection(":memory:")))


def _contribute(service, **overrides):
    kwargs = dict(
        noder_principal="noder-1",
        tenant_id="t1",
        skill=_skill(),
        semver="1.0.0",
        title="Node",
        summary="x",
        visibility=Visibility.PUBLIC,
    )
    kwargs.update(overrides)
    return service.contribute(**kwargs)


# --------------------------------------------------------------------------- #
# Gate unit behaviour.                                                        #
# --------------------------------------------------------------------------- #
def test_gate_flags_mutating_action_as_reserved():
    report = NodeSafetyGate().review(_skill(operation="run").actions)
    assert report.reserved_actions
    assert report.required_backend == "docker"
    assert report.safe is True


def test_gate_treats_read_only_action_as_unreserved():
    report = NodeSafetyGate().review(
        _skill(operation="read").actions, requires_approval=False
    )
    assert report.reserved_actions == []
    assert report.safe is True


def test_gate_rejects_non_isolated_backend():
    report = NodeSafetyGate().review(_skill().actions, backend="subprocess")
    assert report.safe is False
    assert any("isolated" in v for v in report.violations)


def test_gate_rejects_reserved_without_approval():
    report = NodeSafetyGate().review(
        _skill(operation="deploy").actions, requires_approval=False
    )
    assert report.safe is False
    assert any("approval" in v for v in report.violations)


# --------------------------------------------------------------------------- #
# Enforcement at contribute + publish.                                        #
# --------------------------------------------------------------------------- #
def test_safe_node_contributes_and_publishes():
    service = _service()
    result = _contribute(service)
    assert result.version.backend == "docker"
    assert result.version.requires_approval is True
    published = service.publish(
        result.listing.listing_id, noder_principal="noder-1", tenant_id="t1"
    )
    assert published.status.value == "active"


def test_contribute_rejects_non_isolated_backend():
    service = _service()
    with pytest.raises(SafetyViolation):
        _contribute(service, backend="subprocess")


def test_contribute_rejects_mutating_node_without_approval():
    service = _service()
    with pytest.raises(SafetyViolation):
        _contribute(service, requires_approval=False)


def test_read_only_node_may_skip_approval():
    service = _service()
    result = _contribute(service, skill=_skill(operation="read"), requires_approval=False)
    published = service.publish(
        result.listing.listing_id, noder_principal="noder-1", tenant_id="t1"
    )
    assert published.status.value == "active"


def test_publish_re_gates_the_stored_version():
    service = NodeplaceService(RegistryStore(DurableConnection(":memory:")))
    result = _contribute(service)
    # A stricter gate (no backend is considered isolated) must refuse activation
    # even for an already-stored version.
    from workflow_gps.worker.policy import IsolationPolicy

    strict = NodeplaceService(
        service._store,
        safety_gate=NodeSafetyGate(IsolationPolicy(isolated_backends=frozenset())),
    )
    with pytest.raises(SafetyViolation):
        strict.publish(
            result.listing.listing_id, noder_principal="noder-1", tenant_id="t1"
        )
