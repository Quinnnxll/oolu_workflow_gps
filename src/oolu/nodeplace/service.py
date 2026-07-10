from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from ..skills.contract import NodeContract, Slot
from ..skills.models import ReusableSkill
from .errors import ContributionError, OwnershipError, SafetyViolation
from .models import (
    MAX_LINEAGE_DEPTH,
    LineageRecord,
    Listing,
    ListingStatus,
    Node,
    NodeVersion,
    PricingPolicy,
    Visibility,
)
from .safety import NodeSafetyGate
from .sanitize import sanitize_skill
from .screening import screen_script
from .store import RegistryStore


class ContributionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    node: Node
    version: NodeVersion
    listing: Listing
    policy: PricingPolicy


class NodeplaceService:
    def __init__(
        self, store: RegistryStore, *, safety_gate: NodeSafetyGate | None = None
    ) -> None:
        self._store = store
        self._safety = safety_gate or NodeSafetyGate()

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
        backend: str = "docker",
        requires_approval: bool = True,
        derived_from: str | None = None,
        consumes: list[Slot] | None = None,
        produces: list[Slot] | None = None,
        inputs: list | None = None,  # ValueInput: declared creative holes
    ) -> ContributionResult:
        if visibility == Visibility.PRIVATE:
            raise ContributionError(
                "contribute is opt-in sharing; a private workflow is never published"
            )
        lineage = self._lineage_for(derived_from) if derived_from else []
        # The slot vocabulary the goal assembler plans over: explicit when the
        # noder declares one, otherwise derived from the skill itself (induced
        # parameters -> consumes; artifact validators -> produces).
        if consumes is None or produces is None:
            implied = NodeContract.from_skill(skill)
            consumes = implied.consumes if consumes is None else consumes
            produces = implied.produces if produces is None else produces
        report = self._safety.review(
            skill.actions, backend=backend, requires_approval=requires_approval
        )
        if not report.safe:
            raise SafetyViolation(report.violations)
        # The antivirus screen: every script action's code — whether OoLu
        # wrote it or a developer uploaded it — is checked for obviously
        # hostile patterns BEFORE it is stored, so a malicious upload is
        # refused with the reason instead of reaching the registry. The
        # sandbox and verify-by-execution remain the real walls behind it.
        for action in skill.actions:
            script = (action.parameters or {}).get("script")
            if not script:
                continue
            flags = screen_script(str(script))
            if flags:
                raise SafetyViolation(
                    [
                        "the uploaded function was refused by the safety "
                        "screen — it " + reason
                        for reason in flags
                    ]
                )
        sanitized, content_hash = sanitize_skill(skill)

        node = (
            self._owned_node(node_id, noder_principal, tenant_id) if node_id else None
        )
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
            backend=backend,
            requires_approval=requires_approval,
            lineage=lineage,
        )
        self._store.add_version(version)

        listing = Listing(
            version_id=version.version_id,
            title=title,
            summary=summary,
            tags=tags or [],
            consumes=list(consumes),
            produces=list(produces),
            inputs=list(inputs or []),
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

    def _lineage_for(self, parent_version_id: str) -> list[LineageRecord]:
        """The derivation chain a new version inherits from its parent.

        The direct parent is level 1; the parent's own ancestors shift one
        level deeper, capped at ``MAX_LINEAGE_DEPTH``. A revoked ancestor
        stays in the record — provenance is history, not availability — but
        an unknown parent is refused rather than guessed.
        """
        parent = self._store.get_version(parent_version_id)
        if parent is None:
            raise ContributionError(
                f"derived_from references unknown version '{parent_version_id}'"
            )
        parent_node = self._store.get_node(parent.node_id)
        if parent_node is None:
            raise ContributionError(
                f"derived_from version '{parent_version_id}' has no node"
            )
        lineage = [
            LineageRecord(
                ancestor_version_id=parent.version_id,
                ancestor_noder_principal=parent_node.noder_principal,
                level=1,
            )
        ]
        for record in parent.lineage:
            if record.level + 1 > MAX_LINEAGE_DEPTH:
                continue
            lineage.append(record.model_copy(update={"level": record.level + 1}))
        return lineage

    def publish(
        self, listing_id: str, *, noder_principal: str, tenant_id: str
    ) -> Listing:
        listing = self._store.get_listing(listing_id)
        if listing is None:
            raise ContributionError("no such listing")
        self._assert_owns_version(listing.version_id, noder_principal, tenant_id)
        self._gate_version(listing.version_id)
        active = listing.model_copy(
            update={"status": ListingStatus.ACTIVE, "updated_at": datetime.now(UTC)}
        )
        self._store.update_listing(active)
        return active

    def revoke(self, node_id: str, *, noder_principal: str, tenant_id: str) -> bool:
        node = self._owned_node(node_id, noder_principal, tenant_id)
        if node.revoked_at is not None:
            return False
        self._store.update_node(
            node.model_copy(update={"revoked_at": datetime.now(UTC)})
        )
        return True

    def discover(self, query: str = "") -> list[Listing]:
        return self._store.discover(query)

    def list_own_nodes(self, *, noder_principal: str, tenant_id: str) -> list[Node]:
        return self._store.list_nodes(tenant_id, noder_principal)

    def latest_version(self, node_id: str) -> NodeVersion | None:
        """The node's newest version — its current function."""
        versions = self._store.list_versions(node_id)
        return versions[-1] if versions else None

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

    def _gate_version(self, version_id: str) -> None:
        version = self._store.get_version(version_id)
        if version is None:
            raise ContributionError("no such version")
        skill = ReusableSkill.model_validate_json(version.sanitized_skill_json)
        report = self._safety.review(
            skill.actions,
            backend=version.backend,
            requires_approval=version.requires_approval,
        )
        if not report.safe:
            raise SafetyViolation(report.violations)

    def _existing_version(self, node_id: str, content_hash: str) -> NodeVersion | None:
        for version in self._store.list_versions(node_id):
            if version.content_hash == content_hash:
                return version
        return None
