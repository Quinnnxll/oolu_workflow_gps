from __future__ import annotations

import os

import pytest

from oolu.durable import DurableConnection
from oolu.durable.postgres import PostgresDurableConnection
from oolu.knowledge.scrubbing import is_safe_to_store
from oolu.nodeplace import (
    ContributionError,
    NodeplaceService,
    OwnershipError,
    PricingModel,
    PricingPolicy,
    RegistryStore,
    Visibility,
)
from oolu.skills.models import ActionEvent, ReusableSkill, SkillSignature

PG_DSN = os.environ.get("OOLU_TEST_PG_DSN") or os.environ.get("DATABASE_URL")

_PG_TABLES = ("nodes", "node_versions", "listings", "pricing_policies")


def _new_pg() -> PostgresDurableConnection:
    if not PG_DSN:
        pytest.skip("no PostgreSQL DSN configured")
    try:
        conn = PostgresDurableConnection(PG_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with conn.transaction() as db:
        for table in _PG_TABLES:
            db.execute(f"DROP TABLE IF EXISTS {table}")
    return conn


@pytest.fixture(params=["sqlite", "postgres"])
def service(request):
    if request.param == "sqlite":
        connection = DurableConnection(":memory:")
    else:
        connection = _new_pg()
    try:
        yield NodeplaceService(RegistryStore(connection))
    finally:
        connection.close()


def _skill(*, description="does a thing") -> ReusableSkill:
    return ReusableSkill(
        name="deploy helper",
        description=description,
        signature=SkillSignature(application="shell", adapter="cli"),
        actions=[ActionEvent(correlation_id="c1", adapter="cli", operation="run")],
        credential_refs=["cred-ref-1"],
    )


def _contribute(
    service,
    *,
    principal="noder-1",
    tenant="t-noder",
    visibility=Visibility.PUBLIC,
    skill=None,
):
    return service.contribute(
        noder_principal=principal,
        tenant_id=tenant,
        skill=skill or _skill(),
        semver="1.0.0",
        title="Deploy Helper",
        summary="automates a deploy",
        tags=["deploy", "ops"],
        visibility=visibility,
    )


def test_contribute_creates_sanitized_content_addressed_version(service):
    result = _contribute(service)
    assert result.node.visibility == Visibility.PUBLIC
    assert result.version.content_hash and len(result.version.content_hash) == 64
    assert is_safe_to_store(result.version.sanitized_skill_json)


def test_published_version_is_secret_free(service):
    fake_key = "sk-" + "abcdef0123456789abcd"
    fake_email = "a@" + "b.com"
    leaky = _skill(description=f"use key {fake_key} and email {fake_email}")
    result = _contribute(service, skill=leaky)
    stored = result.version.sanitized_skill_json
    assert fake_key not in stored
    assert fake_email not in stored
    assert is_safe_to_store(stored)


def test_private_visibility_is_rejected_on_contribute(service):
    with pytest.raises(ContributionError):
        _contribute(service, visibility=Visibility.PRIVATE)


def test_active_public_listing_is_discoverable(service):
    result = _contribute(service)
    service.publish(
        result.listing.listing_id, noder_principal="noder-1", tenant_id="t-noder"
    )
    found = service.discover("deploy")
    assert [listing.version_id for listing in found] == [result.version.version_id]


def test_draft_listing_is_not_discoverable(service):
    _contribute(service)
    assert service.discover("deploy") == []


def test_unlisted_node_is_not_discoverable_but_retrievable(service):
    result = _contribute(service, visibility=Visibility.UNLISTED)
    service.publish(
        result.listing.listing_id, noder_principal="noder-1", tenant_id="t-noder"
    )
    assert service.discover("deploy") == []
    assert service.runnable_version(result.version.version_id) is not None


def test_revocation_removes_from_discovery(service):
    result = _contribute(service)
    service.publish(
        result.listing.listing_id, noder_principal="noder-1", tenant_id="t-noder"
    )
    assert service.discover("deploy")
    assert (
        service.revoke(
            result.node.node_id, noder_principal="noder-1", tenant_id="t-noder"
        )
        is True
    )
    assert service.discover("deploy") == []
    assert service.runnable_version(result.version.version_id) is None


def test_revoke_is_idempotent(service):
    result = _contribute(service)
    assert (
        service.revoke(
            result.node.node_id, noder_principal="noder-1", tenant_id="t-noder"
        )
        is True
    )
    assert (
        service.revoke(
            result.node.node_id, noder_principal="noder-1", tenant_id="t-noder"
        )
        is False
    )


def test_only_owner_can_revoke(service):
    result = _contribute(service)
    with pytest.raises(OwnershipError):
        service.revoke(
            result.node.node_id, noder_principal="intruder", tenant_id="t-other"
        )


def test_only_owner_can_publish(service):
    result = _contribute(service)
    with pytest.raises(OwnershipError):
        service.publish(
            result.listing.listing_id, noder_principal="intruder", tenant_id="t-other"
        )


def test_same_content_reuses_version(service):
    skill = _skill()
    first = _contribute(service, skill=skill)
    second = service.contribute(
        noder_principal="noder-1",
        tenant_id="t-noder",
        skill=skill,
        semver="1.0.1",
        title="Deploy Helper",
        summary="again",
        node_id=first.node.node_id,
    )
    assert second.version.version_id == first.version.version_id


def test_pricing_policy_is_stored_against_the_version(service):
    result = service.contribute(
        noder_principal="noder-1",
        tenant_id="t-noder",
        skill=_skill(),
        semver="1.0.0",
        title="Paid Node",
        summary="per success",
        pricing=PricingPolicy(
            version_id="ignored", model=PricingModel.PER_SUCCESS, unit_price=0.5
        ),
    )
    assert result.policy.version_id == result.version.version_id
    assert result.policy.model == PricingModel.PER_SUCCESS
    assert result.policy.unit_price == 0.5


def test_list_own_nodes_is_scoped_to_the_noder(service):
    _contribute(service, principal="noder-1", tenant="t-noder")
    _contribute(service, principal="noder-2", tenant="t-other")
    mine = service.list_own_nodes(noder_principal="noder-1", tenant_id="t-noder")
    assert len(mine) == 1
    assert mine[0].noder_principal == "noder-1"
