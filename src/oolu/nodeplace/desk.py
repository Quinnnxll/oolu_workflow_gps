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
    def account_for(self, node_id: str) -> NodeAccount | None:
        return self._accounts.get(node_id)

    def create_account(
        self,
        node_id: str,
        *,
        principal: str,
        tenant: str,
        is_supernode: bool = False,
        supernode_id: str | None = None,
        audit_mode: bool = False,
        allow_autodev_data: bool = True,
        authority_level: int | None = None,
        admin: str | None = None,
        policy_version: str = "",
    ) -> NodeAccount:
        """A node's regime is decided ONCE, at creation.

        Whether it is a Supernode, which Supernode it lives under (the
        creator must own that Supernode), whether it audits, and whether
        its data may feed auto-development are all fixed here and can never
        be changed later. Authority exists only under a Supernode, and a
        Supernode itself always audits — humans in full control.
        """
        node = self._registry.get_node(node_id)
        if node is None or node.tenant_id != tenant:
            raise ContributionError(f"no node '{node_id}'")
        if self._accounts.get(node_id) is not None:
            raise OwnershipError(
                "this node already has an account — its regime was fixed "
                "when it was created"
            )
        # Supernodes nest: a division's Supernode can live under a
        # ministry's, carrying an authority level like any member.
        if authority_level is not None and not supernode_id:
            raise ValueError(
                "authority levels exist only under a Supernode — a "
                "standalone node has no authority"
            )
        if supernode_id:
            parent = self._accounts.get(supernode_id)
            if parent is None or not parent.is_supernode:
                raise ContributionError(f"no Supernode '{supernode_id}'")
            if principal not in {parent.responsible, parent.admin}:
                raise OwnershipError(
                    "you must own the Supernode to create nodes under it"
                )
        # A node created under a Supernode gets NO responsible account at
        # creation: its regime is fixed here, but who answers for it is
        # decided by whoever onboards with their user account. Until then
        # the node id is the claim ticket — callers must keep it private.
        # A Supernode itself always keeps its creator responsible (humans
        # in full control cannot mean nobody).
        unclaimed = bool(supernode_id) and not is_supernode
        account = NodeAccount(
            node_id=node_id,
            responsible="" if unclaimed else principal,
            admin=admin or None,
            is_supernode=is_supernode,
            supernode_id=supernode_id or None,
            # Humans in full control: a Supernode always audits.
            audit_mode=True if is_supernode else audit_mode,
            allow_autodev_data=allow_autodev_data,
            authority_level=authority_level,
            # Which Node Policy was agreed upfront — the public create
            # door refuses creation without the agreement; internal
            # callers stamp what they accepted.
            policy_version=policy_version,
        )
        self._accounts.upsert(account)
        return account

    def onboard_account(
        self, node_id: str, *, principal: str, tenant: str
    ) -> NodeAccount:
        """Take responsibility for an existing node — with NO choices.

        Audit, auto-growing, Supernode membership, and authority were fixed
        when the node was created; onboarding only answers "who is
        responsible now". A node created under a Supernode arrives with its
        regime and authority already set; a node with no account at all
        gets the standalone defaults.
        """
        node = self._registry.get_node(node_id)
        if node is None or node.tenant_id != tenant:
            raise ContributionError(f"no node '{node_id}'")
        current = self._accounts.get(node_id)
        if current is None:
            account = NodeAccount(node_id=node_id, responsible=principal)
            self._accounts.upsert(account)
            return account
        if not current.responsible:
            # The node was created under a Supernode with no responsible:
            # onboarding is the claim. The user account that presents the
            # node id becomes the answering principal — which is why the
            # id must stay private until the intended person has onboarded.
            account = current.model_copy(
                update={"responsible": principal, "updated_at": datetime.now(UTC)}
            )
            self._accounts.upsert(account)
            return account
        if principal in {current.responsible, current.admin, node.noder_principal}:
            return current  # already yours; onboarding is idempotent
        raise OwnershipError(
            "this node already has a responsible — taking over someone "
            "else's live account is not self-service"
        )

    def update_account(
        self,
        node_id: str,
        *,
        principal: str,
        tenant: str,
        status: str | None = None,
        admin: str | None = None,
    ) -> NodeAccount:
        """The mutable slice of an account: status and admin. NOTHING else.

        Authority level, Supernode membership, audit, and auto-growing were
        all fixed at creation — for everyone, the Supernode's humans
        included — and are refused here by construction: this method simply
        has no parameters for them."""
        node = self._registry.get_node(node_id)
        if node is None or node.tenant_id != tenant:
            raise ContributionError(f"no node '{node_id}'")
        current = self._accounts.get(node_id)
        if current is None:
            raise ContributionError(f"node '{node_id}' has no account yet")
        allowed = {node.noder_principal, current.responsible}
        if current.admin:
            allowed.add(current.admin)
        if principal not in allowed:
            raise OwnershipError(
                "only the node's owner, responsible, or admin may change its account"
            )
        account = current.model_copy(
            update={
                "status": current.status if status is None else NodeStatus(status),
                "admin": current.admin if admin is None else (admin or None),
                "updated_at": datetime.now(UTC),
            }
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
