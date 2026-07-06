"""The Work desk: what a noder sees when managing their nodes.

One service composes the operator view the Work environment renders:

- ``overview`` — every node the noder answers for, with its account
  (responsible/admin/authority/status/audit), cumulative earnings (billing
  entries joined through metering events to the node's versions), and
  health (platform-verified successes vs failures — never self-reported).
- ``activity`` — the node's execution history as steps: every run bound to
  one of its versions, expanded into that run's audit entries, so the
  responsible human can follow exactly what the node did and answer for it.
- ``save_account`` / ``onboard`` — create a node's account as its admin, or
  take responsibility for an existing node.

Everything here reads stores that already exist; the desk owns no data of
its own except the accounts table.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from .accounts import NodeAccount, NodeAccountStore, NodeStatus
from .errors import ContributionError, OwnershipError


class NodeHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    verified_successes: int = 0
    verified_failures: int = 0
    # successes / (successes + failures); None until anything is verified.
    score: float | None = None


class DeskEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_id: str
    title: str
    status: str
    account: NodeAccount
    earnings_micros: int = 0
    health: NodeHealth = Field(default_factory=NodeHealth)


class RunSteps(BaseModel):
    """One bound run, expanded into its audit steps."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    gross: float = 0.0
    steps: list[dict] = Field(default_factory=list)  # {seq, event_type, at}


class WorkDesk:
    def __init__(
        self,
        *,
        registry,  # nodeplace.RegistryStore
        accounts: NodeAccountStore,
        billing=None,  # billing.BillingService — earnings
        metering=None,  # metering.MeteringLedger — event -> version join
        stats=None,  # economics.StatsSource — verified health
        attribution=None,  # metering.AttributionStore — node -> runs
        audit=None,  # durable audit log — run -> steps
    ):
        self._registry = registry
        self._accounts = accounts
        self._billing = billing
        self._metering = metering
        self._stats = stats
        self._attribution = attribution
        self._audit = audit

    # ------------------------------------------------------------------ #
    # The node account list.                                              #
    # ------------------------------------------------------------------ #
    def overview(self, *, principal: str, tenant: str) -> list[DeskEntry]:
        nodes = self._registry.list_nodes(tenant, principal)
        versions_by_node = {
            node.node_id: [
                v.version_id for v in self._registry.list_versions(node.node_id)
            ]
            for node in nodes
        }
        owned = {v for ids in versions_by_node.values() for v in ids}
        earnings_by_version = self._earnings_by_version(principal, owned)
        entries: list[DeskEntry] = []
        for node in nodes:
            version_ids = versions_by_node[node.node_id]
            account = self._accounts.get(node.node_id) or NodeAccount(
                node_id=node.node_id, responsible=node.noder_principal
            )
            entries.append(
                DeskEntry(
                    node_id=node.node_id,
                    title=self._title(node, version_ids),
                    status=account.status.value,
                    account=account,
                    earnings_micros=sum(
                        earnings_by_version.get(v, 0) for v in version_ids
                    ),
                    health=self._health(version_ids),
                )
            )
        return entries

    def _title(self, node, version_ids: list[str]) -> str:
        # The newest version's listing carries the human name.
        for version_id in reversed(version_ids):
            listing = self._registry.listing_for_version(version_id)
            if listing is not None:
                return listing.title
        return node.skill_id

    def _earnings_by_version(
        self, principal: str, owned: set[str]
    ) -> dict[str, int]:
        """The noder's cumulative earnings, keyed by their versions that
        earned them: billing entry -> metering event -> the run's bound
        participants. A run that used several of the noder's nodes splits
        the entry evenly among them (exact for the single-node case)."""
        if self._billing is None or self._metering is None:
            return {}
        events_by_id = {e.event_id: e for e in self._metering.events()}
        totals: dict[str, int] = {}
        for entry in self._billing.entries(principal):
            event = events_by_id.get(entry.event_id or "")
            if event is None:
                continue
            participants: list[str] = []
            if self._attribution is not None:
                binding = self._attribution.get_binding(event.run_id)
                if binding is not None:
                    participants = binding.version_ids or [binding.version_id]
            if not participants and event.version_id is not None:
                participants = [event.version_id]
            mine = [v for v in participants if v in owned]
            for version_id in mine:
                totals[version_id] = totals.get(version_id, 0) + (
                    entry.amount_micros // len(mine)
                )
        return totals

    def _health(self, version_ids: list[str]) -> NodeHealth:
        if self._stats is None:
            return NodeHealth()
        successes = failures = 0
        for version_id in version_ids:
            stats = self._stats.version_stats(version_id)
            successes += stats.successes
            failures += stats.failures
        total = successes + failures
        return NodeHealth(
            verified_successes=successes,
            verified_failures=failures,
            score=(successes / total) if total else None,
        )

    # ------------------------------------------------------------------ #
    # Accountability: create-as-admin / onboard-as-responsible.           #
    # ------------------------------------------------------------------ #
    def save_account(
        self,
        node_id: str,
        *,
        principal: str,
        tenant: str,
        admin: str | None = None,
        authority_level: int | None = None,
        status: str | None = None,
        audit_mode: bool | None = None,
        allow_autodev_data: bool | None = None,
    ) -> NodeAccount:
        """Create or update a node's account.

        The node's owner, its current responsible, or its admin may write.
        A node with no account yet can be ONBOARDED: the caller becomes the
        responsible — taking accountability is self-service; taking over
        someone else's live account is not.
        """
        node = self._registry.get_node(node_id)
        if node is None or node.tenant_id != tenant:
            raise ContributionError(f"no node '{node_id}'")
        current = self._accounts.get(node_id)
        allowed = {node.noder_principal}
        if current is not None:
            allowed.add(current.responsible)
            if current.admin:
                allowed.add(current.admin)
        if current is not None and principal not in allowed:
            raise OwnershipError(
                "only the node's owner, responsible, or admin may change its account"
            )
        base = current or NodeAccount(node_id=node_id, responsible=principal)
        account = NodeAccount(
            node_id=node_id,
            responsible=principal if current is None else base.responsible,
            admin=base.admin if admin is None else (admin or None),
            authority_level=(
                base.authority_level if authority_level is None else authority_level
            ),
            status=base.status if status is None else NodeStatus(status),
            audit_mode=base.audit_mode if audit_mode is None else audit_mode,
            allow_autodev_data=(
                base.allow_autodev_data
                if allow_autodev_data is None
                else allow_autodev_data
            ),
            updated_at=datetime.now(UTC),
        )
        self._accounts.upsert(account)
        return account

    # ------------------------------------------------------------------ #
    # The node's execution feed.                                          #
    # ------------------------------------------------------------------ #
    def activity(
        self, node_id: str, *, tenant: str, limit: int = 20
    ) -> list[RunSteps]:
        node = self._registry.get_node(node_id)
        if node is None or node.tenant_id != tenant:
            raise ContributionError(f"no node '{node_id}'")
        if self._attribution is None:
            return []
        version_ids = [
            v.version_id for v in self._registry.list_versions(node_id)
        ]
        bindings = self._attribution.bindings_for_versions(version_ids)[-limit:]
        feed: list[RunSteps] = []
        for binding in bindings:
            steps: list[dict] = []
            if self._audit is not None:
                steps = [
                    {
                        "seq": record.seq,
                        "event_type": record.event_type,
                        "at": record.at.isoformat(),
                    }
                    for record in self._audit.records(run_id=binding.run_id)
                ]
            feed.append(
                RunSteps(run_id=binding.run_id, gross=binding.gross, steps=steps)
            )
        return feed

    # ------------------------------------------------------------------ #
    # Audit-mode enforcement hook.                                        #
    # ------------------------------------------------------------------ #
    def autodev_blocked(self, version_ids: list[str]) -> set[str]:
        """Which of these versions belong to nodes that forbid using run
        data for auto-development — their steps must never be recorded."""
        blocked: set[str] = set()
        for version_id in version_ids:
            version = self._registry.get_version(version_id)
            if version is None:
                continue
            account = self._accounts.get(version.node_id)
            if account is not None and not account.allow_autodev_data:
                blocked.add(version_id)
        return blocked

    def audit_holds_for(self, version_ids: list[str]) -> list[str]:
        """Which of these versions belong to audit-mode nodes — the reasons
        a contract run must be held for a manual commit."""
        reasons: list[str] = []
        for version_id in version_ids:
            version = self._registry.get_version(version_id)
            if version is None:
                continue
            account = self._accounts.get(version.node_id)
            if account is not None and account.audit_mode:
                reasons.append(f"audit-node:{version.node_id}")
        return sorted(set(reasons))
