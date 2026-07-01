from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from ..skills.models import ReusableSkill
from .errors import ContributionError, OwnershipError
from .models import (
    Listing,
    ListingStatus,
    Node,
    NodeVersion,
    PricingPolicy,
    Visibility,
)
from .sanitize import sanitize_skill
from .store import RegistryStore


class ContributionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    node: Node
    version: NodeVersion
    listing: Listing
    policy: PricingPolicy


class NodeplaceService:
    def __init__(self, store: RegistryStore) -> None:
        self._store = store

    def contribute(
        self,
        *,
        noder_principal: str,
        tenant_id: str,
        skill: ReusableSkill,
        semver: str,
        title: str,
        summary: str,
        tags: list[str] | None = None,
        license: str = "proprietary",
        visibility: Visibility = Visibility.PUBLIC,
        pricing: PricingPolicy | None = None,
        node_id: str | None = None,
    ) -> ContributionResult:
        if visibility == Visibility.PRIVATE:
            raise ContributionError(
                "contribute is opt-in sharing; a private workflow is never published"
            )
        sanitized, content_hash = sanitize_skill(skill)

        node = self._owned_node(node_id, noder_principal, tenant_id) if node_id else None
        if node is None:
            node = Node(
                noder_principal=noder_principal,
                tenant_id=tenant_id,
                skill_id=skill.id,
                visibility=visibility,
            )
            self._store.add_node(node)

        existing = self._existing_version(node.node_id, content_hash)
        if existing is not None:
            listing = self._store.listing_for_version(existing.version_id)
            policy = self._store.get_pricing(existing.version_id)
            assert listing is not None and policy is not None
            return ContributionResult(
                node=node, version=existing, listing=listing, policy=policy
            )

        version = NodeVersion(
            node_id=node.node_id,
            semver=semver,
            content_hash=content_hash,
            sanitized_skill_json=sanitized,
            license=license,
        )
        self._store.add_version(version)

        listing = Listing(
            version_id=version.version_id,
            title=title,
            summary=summary,
            tags=tags or [],
            status=ListingStatus.DRAFT,
        )
        self._store.add_listing(listing)

        policy = (pricing or PricingPolicy(version_id=version.version_id)).model_copy(
            update={"version_id": version.version_id}
        )
        self._store.add_pricing(policy)
        return ContributionResult(
            node=node, version=version, listing=listing, policy=policy
        )

    def publish(self, listing_id: str, *, noder_principal: str, tenant_id: str) -> Listing:
        listing = self._store.get_listing(listing_id)
        if listing is None:
            raise ContributionError("no such listing")
        self._assert_owns_version(listing.version_id, noder_principal, tenant_id)
        active = listing.model_copy(
            update={"status": ListingStatus.ACTIVE, "updated_at": datetime.now(UTC)}
        )
        self._store.update_listing(active)
        return active

    def revoke(self, node_id: str, *, noder_principal: str, tenant_id: str) -> bool:
        node = self._owned_node(node_id, noder_principal, tenant_id)
        if node.revoked_at is not None:
            return False
        self._store.update_node(node.model_copy(update={"revoked_at": datetime.now(UTC)}))
        return True

    def discover(self, query: str = "") -> list[Listing]:
        return self._store.discover(query)

    def list_own_nodes(self, *, noder_principal: str, tenant_id: str) -> list[Node]:
        return self._store.list_nodes(tenant_id, noder_principal)

    def runnable_version(self, version_id: str) -> NodeVersion | None:
        version = self._store.get_version(version_id)
        if version is None:
            return None
        node = self._store.get_node(version.node_id)
        if node is None or node.revoked_at is not None:
            return None
        if node.visibility == Visibility.PRIVATE:
            return None
        return version

    def _owned_node(self, node_id: str, noder_principal: str, tenant_id: str) -> Node:
        node = self._store.get_node(node_id)
        if node is None:
            raise ContributionError("no such node")
        if node.noder_principal != noder_principal or node.tenant_id != tenant_id:
            raise OwnershipError("not the owning noder")
        return node

    def _assert_owns_version(
        self, version_id: str, noder_principal: str, tenant_id: str
    ) -> None:
        version = self._store.get_version(version_id)
        if version is None:
            raise ContributionError("no such version")
        self._owned_node(version.node_id, noder_principal, tenant_id)

    def _existing_version(self, node_id: str, content_hash: str) -> NodeVersion | None:
        for version in self._store.list_versions(node_id):
            if version.content_hash == content_hash:
                return version
        return None
