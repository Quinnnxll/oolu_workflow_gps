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

from .accounts import (
    NodeAccount,
    NodeAccountStore,
    NodeStatus,
    normalize_blocked_hosts,
    normalize_blocked_users,
    normalize_network_hosts,
)
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
    # The node that EXECUTED this run, by name — set when the feed
    # aggregates a Supernode's members; a node's own feed leaves it None.
    node_title: str | None = None
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
        """Every node this human ANSWERS FOR: the ones they created in the
        registry, plus the ones they onboarded (or admin) whose account
        names them — a claim ticket someone else minted still lands the
        node on the claimer's own desk, not the creator's."""
        nodes = self._registry.list_nodes(tenant, principal)
        listed = {node.node_id for node in nodes}
        for account in self._accounts.answered_by(principal):
            if account.node_id in listed:
                continue
            node = self._registry.get_node(account.node_id)
            if node is None or node.tenant_id != tenant:
                continue
            nodes.append(node)
            listed.add(account.node_id)
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
            if account.deleted_at is not None:
                # A deleted node is OFF the desk — everywhere at once.
                # The tombstone lives for the revival window only.
                continue
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
        # No listing yet: condense the skill id into a readable keyword
        # name ("learned.convert.quarterly.report.pdf" -> "Convert
        # Quarterly Report Pdf") instead of surfacing the raw id.
        from ..naming import concise_name

        skill_id = str(node.skill_id)
        stem = skill_id.split(".", 1)[1] if skill_id.startswith("learned.") else skill_id
        return concise_name(stem.replace(".", " ").replace("_", " ")) or skill_id

    def _earnings_by_version(
        self, principal: str, owned: set[str]
    ) -> dict[str, int]:
        """The noder's cumulative earnings, keyed by their versions that
        earned them: billing entry -> metering event -> the run's bound
        participants. A run that used several of the noder's nodes splits
        the entry evenly among them (exact for the single-node case)."""
        if self._billing is None or self._metering is None:
            return {}
        totals: dict[str, int] = {}
        for entry in self._billing.entries(principal):
            # A key lookup per entry (the noder's own earnings history) —
            # never a materialization of everything ever metered.
            event = (
                self._metering.get_by_event_id(entry.event_id)
                if entry.event_id
                else None
            )
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

    def delete_node(self, node_id: str, *, at) -> bool:
        """Tombstone the node: off every list at once, revivable until
        the window closes, purged for good after."""
        return self._accounts.mark_deleted(node_id, at=at)

    def revive_node(self, node_id: str) -> bool:
        """The administrator's undo — clears the tombstone."""
        return self._accounts.revive(node_id)

    def purge_deleted(self, *, before) -> list[NodeAccount]:
        """Accounts whose revival window has passed, removed for good —
        returned so the caller can take the node's files with them."""
        return self._accounts.purge_deleted(before=before)

    def node_tenant(self, node_id: str) -> str | None:
        """Which tenant a node lives in, off the registry — what the
        purge needs to find the node's drawer."""
        node = self._registry.get_node(node_id)
        return node.tenant_id if node is not None else None

    def siblings(self, node_id: str, *, tenant: str) -> list[dict]:
        """The nodes under the SAME Supernode as this one — the org's
        members a node may message. Same-tenant only, self excluded, each
        as ``{"node_id", "title"}`` with the title the desk would show."""
        account = self._accounts.get(node_id)
        if account is None or not account.supernode_id:
            return []
        members: list[dict] = []
        for member in self._accounts.under(account.supernode_id):
            if member.node_id == node_id:
                continue
            node = self._registry.get_node(member.node_id)
            if node is None or node.tenant_id != tenant:
                continue
            version_ids = [
                v.version_id for v in self._registry.list_versions(node.node_id)
            ]
            members.append(
                {"node_id": node.node_id, "title": self._title(node, version_ids)}
            )
        return members

    def members_of(self, supernode_id: str, *, tenant: str) -> list[dict]:
        """A Supernode's own fleet, same-tenant, each as
        ``{"node_id", "title"}`` — what the template import checks
        against so a role that already sits is never minted twice."""
        members: list[dict] = []
        for member in self._accounts.under(supernode_id):
            node = self._registry.get_node(member.node_id)
            if node is None or node.tenant_id != tenant:
                continue
            version_ids = [
                v.version_id for v in self._registry.list_versions(node.node_id)
            ]
            members.append(
                {"node_id": node.node_id, "title": self._title(node, version_ids)}
            )
        return members

    def deleted_members_of(
        self, supernode_id: str, *, tenant: str
    ) -> list[dict]:
        """A Supernode's RECENTLY DELETED members — the revival list an
        administrator reads to undo an accidental delete before the
        window closes. Same-tenant, tombstoned accounts only."""
        members: list[dict] = []
        for member in self._accounts.under(
            supernode_id, include_deleted=True
        ):
            if member.deleted_at is None:
                continue
            node = self._registry.get_node(member.node_id)
            if node is None or node.tenant_id != tenant:
                continue
            version_ids = [
                v.version_id for v in self._registry.list_versions(node.node_id)
            ]
            members.append(
                {
                    "node_id": node.node_id,
                    "title": self._title(node, version_ids),
                    "deleted_at": member.deleted_at.isoformat(),
                }
            )
        return members

    def describe(self, node_id: str, *, tenant: str) -> str:
        """The node's human description — its title plus the newest
        listing's summary. What the template matcher reads."""
        node = self._registry.get_node(node_id)
        if node is None or node.tenant_id != tenant:
            return ""
        version_ids = [
            v.version_id for v in self._registry.list_versions(node_id)
        ]
        title = self._title(node, version_ids)
        summary = ""
        for version_id in reversed(version_ids):
            listing = self._registry.listing_for_version(version_id)
            if listing is not None:
                summary = listing.summary
                break
        return " — ".join(p for p in (title, summary) if p)

    def mark_verified(self, node_id: str) -> NodeAccount | None:
        """The verification door: a node whose own function completed a
        real, audited run stops being 'needs_verification' and goes live.
        ONLY that one transition — error and restricted states are never
        silently healed by a passing run, and an already-live node is
        left untouched."""
        account = self._accounts.get(node_id)
        if account is None or account.status is not NodeStatus.NEEDS_VERIFICATION:
            return account
        promoted = account.model_copy(
            update={"status": NodeStatus.LIVE, "updated_at": datetime.now(UTC)}
        )
        self._accounts.upsert(promoted)
        return promoted

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
            # Humans in full control: an org's ROOT Supernode always
            # audits — but under it, the owner chooses. Not every node
            # created under a Supernode needs a human countersigning
            # every run; nested divisions and members take the creator's
            # own audit choice.
            audit_mode=(
                True
                if is_supernode and not supernode_id
                else audit_mode
            ),
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
        network_hosts: list[str] | tuple[str, ...] | None = None,
        blocked_hosts: list[str] | tuple[str, ...] | None = None,
        blocked_users: list[str] | tuple[str, ...] | None = None,
    ) -> NodeAccount:
        """The mutable slice of an account: status, admin, the egress
        grant, and the Supernode's block lists. NOTHING else.

        Authority level, Supernode membership, audit, and auto-growing were
        all fixed at creation — for everyone, the Supernode's humans
        included — and are refused here by construction: this method simply
        has no parameters for them. The egress grant IS mutable because it
        is consent — given and withdrawable by the same humans who answer
        for the node — and the block lists are the same consent inverted:
        which hosts an open-web Supernode refuses, and which principals it
        will not hear from."""
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
                "network_hosts": (
                    current.network_hosts
                    if network_hosts is None
                    else normalize_network_hosts(network_hosts)
                ),
                "blocked_hosts": (
                    current.blocked_hosts
                    if blocked_hosts is None
                    else normalize_blocked_hosts(blocked_hosts)
                ),
                "blocked_users": (
                    current.blocked_users
                    if blocked_users is None
                    else normalize_blocked_users(blocked_users)
                ),
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
        """The node's execution feed — and for a Supernode, the FLEET's.

        Every execution touching a member node shows in the Supernode's
        activity, tagged with the executing node's name (managing many
        nodes is the point). Two evidence sources per node: marketplace
        run bindings, and the metering ledger's verified runs — so
        personal executions of a node's own function appear too, not
        only paid ones."""
        node = self._registry.get_node(node_id)
        if node is None or node.tenant_id != tenant:
            raise ContributionError(f"no node '{node_id}'")
        account = self._accounts.get(node_id)
        sources: list[tuple[str, str | None]] = [(node_id, None)]
        if account is not None and account.is_supernode:
            for member in self._accounts.under(node_id):
                m_node = self._registry.get_node(member.node_id)
                if m_node is None or m_node.tenant_id != tenant:
                    continue
                m_versions = [
                    v.version_id
                    for v in self._registry.list_versions(member.node_id)
                ]
                sources.append(
                    (member.node_id, self._title(m_node, m_versions))
                )
        feed: list[RunSteps] = []
        seen: set[str] = set()
        for source_id, title in sources:
            version_ids = [
                v.version_id for v in self._registry.list_versions(source_id)
            ]
            entries: list[tuple[str, float]] = []
            if self._attribution is not None:
                entries += [
                    (b.run_id, b.gross)
                    for b in self._attribution.bindings_for_versions(version_ids)
                ]
            if self._metering is not None:
                known = {run_id for run_id, _ in entries}
                for event in self._metering.events():
                    if event.version_id in version_ids and event.run_id not in known:
                        entries.append((event.run_id, float(event.gross or 0.0)))
            for run_id, gross in entries:
                if run_id in seen:
                    continue
                seen.add(run_id)
                steps: list[dict] = []
                if self._audit is not None:
                    steps = [
                        {
                            "seq": record.seq,
                            "event_type": record.event_type,
                            "at": record.at.isoformat(),
                        }
                        for record in self._audit.records(run_id=run_id)
                    ]
                feed.append(
                    RunSteps(
                        run_id=run_id,
                        gross=gross,
                        node_title=title,
                        steps=steps,
                    )
                )
        # One chronological feed across the fleet, oldest first, capped.
        feed.sort(key=lambda entry: entry.steps[-1]["at"] if entry.steps else "")
        return feed[-limit:]

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

    def network_grants(self, version_ids: list[str]) -> dict[str, tuple[str, ...]]:
        """Each REGISTERED version's egress consent, keyed by version id.

        A version whose node has an account maps to that account's granted
        hosts; registered but never onboarded maps to ``()`` — published
        code nobody answers for gets no egress until someone does. Versions
        the registry does not know are OMITTED, not defaulted: an ad-hoc
        child is the submitter's own request, and the stamping step leaves
        it to the machine policy."""
        grants: dict[str, tuple[str, ...]] = {}
        for version_id in version_ids:
            version = self._registry.get_version(version_id)
            if version is None:
                continue
            account = self._accounts.get(version.node_id)
            grants[version_id] = (
                account.network_hosts if account is not None else ()
            )
        return grants

    def owning_nodes(self, version_ids: list[str]) -> dict[str, str]:
        """Each REGISTERED version's owning node id, keyed by version id —
        the join the open-web verdict needs (per node, not per version).
        Unknown versions are omitted, same as ``network_grants``."""
        owners: dict[str, str] = {}
        for version_id in version_ids:
            version = self._registry.get_version(version_id)
            if version is not None:
                owners[version_id] = version.node_id
        return owners

    def supernode_owned(
        self, node_id: str, *, principal: str, tenant: str
    ) -> NodeAccount:
        """The template door's gate: the node exists here, IS a Supernode,
        and the caller is one of its humans — or the exact refusal."""
        node = self._registry.get_node(node_id)
        if node is None or node.tenant_id != tenant:
            raise ContributionError(f"no node '{node_id}'")
        current = self._accounts.get(node_id)
        if current is None or not current.is_supernode:
            raise ContributionError(
                "org templates belong to Supernodes — this node is not one"
            )
        allowed = {node.noder_principal, current.responsible}
        if current.admin:
            allowed.add(current.admin)
        if principal not in allowed:
            raise OwnershipError(
                "only the Supernode's owner, responsible, or admin may "
                "resolve its template"
            )
        return current

    def record_org_template(
        self, node_id: str, *, principal: str, tenant: str, key: str
    ) -> NodeAccount:
        """Record which org template this Supernode resolved to — ONCE.

        The first resolution wins and sticks: a recorded choice is never
        re-reasoned, which is what keeps the template button deterministic
        (and free) on every later press. Only the Supernode's own humans
        may record it."""
        current = self.supernode_owned(
            node_id, principal=principal, tenant=tenant
        )
        if current.org_template:
            return current  # decided once; recording is idempotent
        account = current.model_copy(
            update={"org_template": key, "updated_at": datetime.now(UTC)}
        )
        self._accounts.upsert(account)
        return account

    def blocked_users_for(self, node_id: str) -> frozenset[str]:
        """Every principal refused along this node's Supernode chain — the
        node's own list plus each Supernode above it (a ministry's block
        binds its divisions). Empty when nobody is blocked."""
        blocked: set[str] = set()
        seen: set[str] = set()
        current_id: str | None = node_id
        while current_id and current_id not in seen:
            seen.add(current_id)
            account = self._accounts.get(current_id)
            if account is None:
                break
            blocked.update(account.blocked_users)
            current_id = account.supernode_id
        return frozenset(blocked)

    # ------------------------------------------------------------------ #
    # The Supernode owner's SOP: an execution order over the fleet.       #
    # ------------------------------------------------------------------ #
    def set_exec_order(
        self,
        node_id: str,
        *,
        principal: str,
        tenant: str,
        order: int | None,
    ) -> NodeAccount:
        """Where a member stands in the org's execution order — the SOP
        dial. Work flows in ascending numbers; members sharing a number
        run in PARALLEL; None clears it (called whenever needed).
        MUTABLE, unlike the trust regime — an SOP is retuned as the org
        learns — but only by the parent Supernode's own humans."""
        node = self._registry.get_node(node_id)
        if node is None or node.tenant_id != tenant:
            raise ContributionError(f"no node '{node_id}'")
        account = self._accounts.get(node_id)
        if account is None or not account.supernode_id:
            raise ValueError(
                "execution order exists only under a Supernode — a "
                "standalone node is always called whenever needed"
            )
        parent = self._accounts.get(account.supernode_id)
        if parent is None or principal not in {
            parent.responsible,
            parent.admin,
        }:
            raise OwnershipError(
                "only the Supernode's own humans set the execution order"
            )
        if order is not None:
            order = int(order)
            if not 1 <= order <= 999:
                raise ValueError(
                    "an execution order is a small step number (1-999) — "
                    "or none at all for a node called whenever needed"
                )
        updated = account.model_copy(
            update={"exec_order": order, "updated_at": datetime.now(UTC)}
        )
        self._accounts.upsert(updated)
        return updated

    def sop_edges_for(
        self, version_ids: list[str]
    ) -> list[tuple[str, str]]:
        """The owners' SOP as explicit dependencies over a contract's
        children: within one Supernode's members, every child in an
        earlier order group runs before every child in the NEXT present
        group — like work passed down a line. Equal numbers share a
        group (parallel); members with no order impose nothing and join
        wherever data needs them. Returns (source, target) version-id
        pairs; different Supernodes' SOPs never entangle."""
        fleets: dict[str, dict[int, list[str]]] = {}
        for version_id in version_ids:
            version = self._registry.get_version(version_id)
            if version is None:
                continue
            account = self._accounts.get(version.node_id)
            if (
                account is None
                or not account.supernode_id
                or account.exec_order is None
            ):
                continue
            fleets.setdefault(account.supernode_id, {}).setdefault(
                account.exec_order, []
            ).append(version_id)
        edges: list[tuple[str, str]] = []
        for ordered in fleets.values():
            steps = sorted(ordered)
            for first, then in zip(steps, steps[1:]):
                for source in ordered[first]:
                    for target in ordered[then]:
                        edges.append((source, target))
        return edges

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
