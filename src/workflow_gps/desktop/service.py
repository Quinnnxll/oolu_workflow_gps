"""The desktop shell's application service — the local loopback boundary.

A desktop UI binds to this service (over a loopback API or named pipe); it never
touches the orchestrator, durable stores, or vault directly. Every workflow action
goes through the backend's own gates: clarification/confirmation/approval/incident
are driven via the durable service's resume path (so the orchestrator's execution
preflight still applies), approvals are minted only from an authorized identity
session, and provider secrets live in the vault and never appear in a view.

There is deliberately **no general execute method here** — the shell cannot run
arbitrary code or bypass policy; it can only request backend operations and
present their results. The one narrow exception is ``confirm_assembly``: it runs
a previewed marketplace contract, but only on the backend-configured executors,
only through the same shared money path as the gateway's ``/v1/runs/contract``,
and never unattended for contracts containing reserved actions — those are HELD
in the inbox as approvable tasks and run only after ``approve_assembly`` mints
an approval from an authorized identity session, like any other approval.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from ..durable.maintenance import delete_workflow
from ..durable.service import DurableWorkflowService
from ..identity.models import Session
from ..identity.service import IdentityApprovalAuthority
from ..nodeplace.holds import PendingContractRecord, PendingContractStore
from ..orchestrator.state import (
    PauseKind,
    Phase,
    ResumeInput,
    RoutePlan,
    RunState,
    TaskContract,
)
from ..providers.vault import SecretVault
from ..worker.leases import TrustLevel
from ..worker.policy import IsolationPolicy
from .views import (
    ActionView,
    AssemblyPayoutView,
    AssemblyPreviewView,
    AssemblyRunStepView,
    AssemblyRunView,
    AssemblyStepView,
    AuditEntryView,
    AuditView,
    BlueprintView,
    ExecutionLabel,
    ExportBundle,
    InboxItem,
    ProviderConnectionView,
    QuestionView,
    RoutePreview,
    TaskView,
    TimelineEvent,
    WorkerHealthView,
)

_PAUSE_LABELS = {
    PauseKind.CLARIFICATION: "clarification",
    PauseKind.CONFIRMATION: "confirmation",
    PauseKind.APPROVAL: "approval",
    PauseKind.INCIDENT: "incident",
}


class _Connection:
    __slots__ = (
        "connection_id",
        "provider",
        "credential_ref",
        "scopes",
        "status",
        "connected_at",
    )

    def __init__(self, connection_id, provider, credential_ref, scopes, connected_at):
        self.connection_id = connection_id
        self.provider = provider
        self.credential_ref = credential_ref
        self.scopes = scopes
        self.status = "connected"
        self.connected_at = connected_at


class DesktopService:
    def __init__(
        self,
        durable: DurableWorkflowService,
        *,
        approval_authority: IdentityApprovalAuthority | None = None,
        vault: SecretVault | None = None,
        isolation: IsolationPolicy | None = None,
        docker_available: bool = True,
        market=None,  # nodeplace.CandidateAssembler, when the shell has one
        price_book=None,  # nodeplace.PriceBook
        contract_runner=None,  # orchestrator.DagRouteRunner over backend executors
        attribution=None,  # metering.AttributionStore
        trace_store=None,  # knowledge.TraceStore: confirmed runs sharpen picks
        rng=None,  # random.Random for explore-mode assembly; seedable
        wallet_lookup=None,  # () -> the LINKED wallet's remaining balance | None
        session_manager=None,  # identity.SessionManager: loopback decisions
        hold_ttl_seconds=None,  # held contracts expire after this; None=never
        clock=None,  # () -> datetime, injectable for tests
    ):
        self._durable = durable
        self._approval = approval_authority
        self._vault = vault or SecretVault()
        self._isolation = isolation or IsolationPolicy()
        self._docker_available = docker_available
        self._market = market
        self._price_book = price_book
        self._contract_runner = contract_runner
        self._attribution = attribution
        self._trace_store = trace_store
        self._rng = rng or random.Random()
        # A partial view of the user's assets by design: budgets never cap
        # on the linked balance, they only flag it for review.
        self._wallet_lookup = wallet_lookup
        # Lets the loopback turn a bearer token into a verified Session for
        # approval decisions — the loopback itself never trusts caller text.
        self._sessions = session_manager
        self._connections: dict[str, _Connection] = {}
        self._confirmed: dict[str, AssemblyRunView] = {}
        # Reserved contracts held for approval live in the durable store —
        # a hold survives a shell restart. The compiled artifact is a
        # process-local cache: whichever process decides recompiles once.
        # With a TTL, stale holds are swept lazily on every inbox/decision.
        self._pending = PendingContractStore(durable.conn)
        self._compiled_holds: dict[str, tuple[Any, Any]] = {}
        self._hold_ttl_seconds = hold_ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ #
    # Task entry + guided clarification.                                  #
    # ------------------------------------------------------------------ #
    def submit_task(
        self,
        intent: str,
        *,
        submitted_by: str = "local-user",
        max_recovery_attempts: int = 1,
    ) -> TaskView:
        contract = TaskContract(intent=intent, submitted_by=submitted_by)
        state = self._durable.submit(
            contract, max_recovery_attempts=max_recovery_attempts
        )
        return self._task_view(state)

    def answer_questions(self, run_id: str, answers: dict[str, Any]) -> TaskView:
        state = self._durable.resume(
            run_id, ResumeInput(kind=PauseKind.CLARIFICATION, answers=answers)
        )
        return self._task_view(state)

    def task(self, run_id: str) -> TaskView:
        return self._task_view(self._require(run_id))

    # ------------------------------------------------------------------ #
    # Route preview.                                                      #
    # ------------------------------------------------------------------ #
    def route_preview(self, run_id: str) -> RoutePreview:
        state = self._require(run_id)
        if state.route is None:
            return RoutePreview(run_id=run_id)
        exclusions = [
            {"name": bp.name, "reason": bp.exclusion_reason or "excluded"}
            for bp in [state.route.chosen, *state.route.alternatives]
            if bp.excluded
        ]
        return RoutePreview(
            run_id=run_id,
            chosen=self._blueprint_view(state.route),
            alternatives=[
                self._blueprint_view_of(bp) for bp in state.route.alternatives
            ],
            total_cost=state.route.total_cost,
            exclusions=exclusions,
        )

    # ------------------------------------------------------------------ #
    # Inboxes: confirmation, approval, incident.                          #
    # ------------------------------------------------------------------ #
    def inbox(self, kind: str | None = None) -> list[InboxItem]:
        items: list[InboxItem] = []
        for state in self._durable.runs.list():
            if state.pause is None:
                continue
            pause_kind = _PAUSE_LABELS[state.pause.kind]
            if kind is not None and pause_kind != kind:
                continue
            items.append(
                InboxItem(
                    run_id=state.run_id,
                    kind=pause_kind,
                    intent=state.intent,
                    prompt=state.pause.prompt,
                    created_at=state.pause.created_at,
                )
            )
        # Reserved contracts held by confirm_assembly: approvable tasks,
        # not dead ends — durable, so they survive a shell restart.
        # Decided via approve_assembly (identity-gated).
        if kind is None or kind == "contract-approval":
            self._sweep_holds()
            for record in self._pending.list():
                items.append(
                    InboxItem(
                        run_id=record.pending_id,
                        kind="contract-approval",
                        intent=str(record.contract.get("name", "contract")),
                        prompt=(
                            "contract contains reserved actions ("
                            + ", ".join(record.reserved)
                            + "); approve to run it"
                        ),
                        created_at=record.created_at,
                    )
                )
        return items

    def confirm(self, run_id: str, *, approved: bool) -> TaskView:
        state = self._durable.resume(
            run_id, ResumeInput(kind=PauseKind.CONFIRMATION, confirmed=approved)
        )
        return self._task_view(state)

    def approve(
        self, run_id: str, *, session: Session, required_assurance: int = 1
    ) -> TaskView:
        """Approve via a verified identity session — never from caller text.

        The approval record is minted only if the session is authorized; an
        unauthorized session raises and the run is not advanced.
        """
        if self._approval is None:
            raise RuntimeError("no approval authority configured")
        state = self._require(run_id)
        if state.pause is None or state.pause.kind is not PauseKind.APPROVAL:
            raise RuntimeError("run is not awaiting approval")
        policy = state.route.chosen.name if state.route else "execute"
        record = self._approval.approve(
            session,
            run_id=run_id,
            policy=policy,
            requester_id=state.contract.submitted_by,
            required_assurance=required_assurance,
        )
        state = self._durable.resume(
            run_id, ResumeInput(kind=PauseKind.APPROVAL, approvals=[record])
        )
        return self._task_view(state)

    def resolve_incident(self, run_id: str, *, decision: str) -> TaskView:
        state = self._durable.resume(
            run_id, ResumeInput(kind=PauseKind.INCIDENT, incident_decision=decision)
        )
        return self._task_view(state)

    # ------------------------------------------------------------------ #
    # Timeline, cancellation, audit.                                      #
    # ------------------------------------------------------------------ #
    def timeline(self, run_id: str) -> list[TimelineEvent]:
        state = self._require(run_id)
        events = [
            TimelineEvent(
                at=t.at,
                label=f"{t.from_phase.value} → {t.to_phase.value}",
                detail=t.note,
            )
            for t in state.history
        ]
        return events

    def cancel(self, run_id: str) -> TaskView:
        state = self._require(run_id)
        if not state.is_terminal:
            state.phase = Phase.CANCELLED
            state.failure_reason = "cancelled by user"
            state.pause = None
            state.updated_at = datetime.now(UTC)
            self._durable.runs.save(state)
            self._durable.audit.append("workflow.cancelled", {"run_id": run_id})
        return self._task_view(state)

    def audit(self, run_id: str) -> AuditView:
        history = self._durable.reconstruct_history(run_id)
        return AuditView(
            run_id=run_id,
            verified=bool(history["audit_verified"]),
            entries=[
                AuditEntryView(seq=r.seq, event_type=r.event_type, at=r.at)
                for r in history["audit"]
            ],
        )

    # ------------------------------------------------------------------ #
    # Provider connection management (OS credential vault stand-in).      #
    # ------------------------------------------------------------------ #
    def connect_provider(
        self,
        provider: str,
        secret: str,
        *,
        scopes: list[str] | None = None,
    ) -> ProviderConnectionView:
        ref = self._vault.put(secret, kind=f"{provider}_credential")
        connection = _Connection(
            connection_id=uuid4().hex,
            provider=provider,
            credential_ref=ref,
            scopes=list(scopes or []),
            connected_at=datetime.now(UTC),
        )
        self._connections[connection.connection_id] = connection
        return self._connection_view(connection)

    def list_connections(self) -> list[ProviderConnectionView]:
        return [self._connection_view(c) for c in self._connections.values()]

    def disconnect(self, connection_id: str) -> ProviderConnectionView:
        connection = self._connections.get(connection_id)
        if connection is None:
            raise KeyError(f"unknown connection: {connection_id}")
        self._vault.revoke(connection.credential_ref)
        connection.status = "disconnected"
        return self._connection_view(connection)

    # ------------------------------------------------------------------ #
    # Assembly preview: the plan, its prices, its payees — before running. #
    # ------------------------------------------------------------------ #
    def assembly_preview(
        self,
        *,
        goal: str,
        want: list[dict[str, Any]],
        have: list[dict[str, Any]] | None = None,
        query: str = "",
        fill_gaps: bool = False,
        explore: bool = False,
        budget_cap: float | None = None,
        review_threshold: float | None = None,
    ) -> AssemblyPreviewView:
        """The assembly screen's data: one call, everything a non-developer
        needs to decide — which nodes were picked, what each costs (with the
        clearing forces spelled out), and exactly who gets paid on verified
        success. Read-only: no price commits, no ledger writes, and the
        returned ``contract`` is the runnable artifact the user confirms.
        """
        if self._market is None or self._price_book is None:
            raise KeyError("market economics are not configured for this shell")
        # Imported lazily so a shell without marketplace features never pays
        # the nodeplace import.
        from ..nodeplace.assembly import preview_assembly
        from ..nodeplace.budget import BudgetPolicy
        from ..orchestrator.assembler import GoalSpec

        spec = GoalSpec.model_validate({"name": goal, "want": want, "have": have or []})
        preview = preview_assembly(
            self._market,
            self._price_book,
            spec,
            query=query,
            fill_gaps=fill_gaps,
            # Picks carry this desktop's own confirmed-run history on top
            # of platform-verified counts (single user: the global bucket).
            trace_store=self._trace_store,
            # explore: Thompson-sample picks so unproven alternatives get
            # real chances proportional to their remaining uncertainty.
            rng=self._rng if explore else None,
            budget=BudgetPolicy(hard_cap=budget_cap, review_threshold=review_threshold),
            spend_lookup=self._spend_history,
            wallet_balance=self._wallet_balance(),
        )
        return AssemblyPreviewView(
            goal=goal,
            complete=preview.complete,
            selected=list(preview.selected),
            gap_filled=list(preview.gap_filled),
            missing=[slot.name for slot in preview.missing],
            steps=[
                AssemblyStepView(
                    name=node.name,
                    kind=node.kind,
                    gap=node.gap,
                    version_id=node.version_id,
                    price=(node.cleared or {}).get("cleared"),
                    price_notes=list((node.cleared or {}).get("notes", [])),
                    payouts=[
                        AssemblyPayoutView(noder=p.noder_principal, amount=p.amount)
                        for p in node.payout_previews
                    ],
                )
                for node in preview.nodes
            ],
            estimated_gross_total=preview.estimated_gross_total,
            platform_margin_preview=preview.platform_margin_preview,
            contract=(
                preview.contract.model_dump(mode="json")
                if preview.contract is not None
                else None
            ),
            learned_order=list(preview.learned_order),
            budget=(
                preview.budget.model_dump(mode="json")
                if preview.budget is not None
                else None
            ),
        )

    def _spend_history(self, goal_class: str | None = None) -> list[float] | None:
        if self._attribution is None:
            return None
        return self._attribution.consumer_spend(
            "local", "desktop", goal_class=goal_class
        )

    def _wallet_balance(self) -> float | None:
        return self._wallet_lookup() if self._wallet_lookup is not None else None

    def confirm_assembly(
        self,
        contract: dict[str, Any],
        *,
        confirm_id: str | None = None,
        budget_cap: float | None = None,
        review_threshold: float | None = None,
        review_acknowledged: bool = False,
    ) -> AssemblyRunView:
        """The confirm button: run the contract the preview returned.

        Executes through the same shared money path as the gateway's
        ``POST /v1/runs/contract`` — every marketplace node clears at a
        committed price, the run binds once with the lineage-weighted
        aggregate split, and earnings accrue only on platform-verified
        success. A contract containing reserved actions is not refused
        here — it is HELD: the view comes back ``awaiting_approval`` and
        the contract lands in the inbox (kind ``contract-approval``),
        runnable only through :meth:`approve_assembly` with an authorized
        identity session. Executors are backend-configured, never
        UI-supplied. A ``confirm_id`` makes the click idempotent: replays
        return the first result without running anything twice.
        """
        if (
            self._market is None
            or self._price_book is None
            or self._contract_runner is None
            or self._attribution is None
        ):
            raise KeyError("contract execution is not configured for this shell")
        if confirm_id and confirm_id in self._confirmed:
            return self._confirmed[confirm_id]
        # Imported lazily so a shell without marketplace features never pays
        # the nodeplace import.
        from ..nodeplace.execution import compile_contract, reserved_operations
        from ..skills.contract import NodeContract

        parsed = NodeContract.model_validate(contract)
        compiled = compile_contract(parsed)
        reserved = reserved_operations(compiled)
        if reserved:
            # Not a dead end: hold it durably for an authorized approver.
            pending_id = uuid4().hex
            now = self._clock()
            self._pending.add(
                PendingContractRecord(
                    pending_id=pending_id,
                    contract=parsed.model_dump(mode="json"),
                    reserved=reserved,
                    consumer_tenant="local",
                    consumer_principal="desktop",
                    budget_cap=budget_cap,
                    review_threshold=review_threshold,
                    review_acknowledged=review_acknowledged,
                    created_at=now,
                    expires_at=(
                        now + timedelta(seconds=self._hold_ttl_seconds)
                        if self._hold_ttl_seconds is not None
                        else None
                    ),
                )
            )
            self._compiled_holds[pending_id] = (parsed, compiled)
            self._durable.audit.append(
                "contract.held",
                {"pending_id": pending_id, "name": parsed.name, "reserved": reserved},
            )
            view = AssemblyRunView(run_id=pending_id, status="awaiting_approval")
            if confirm_id:
                self._confirmed[confirm_id] = view
            return view
        self._enforce_contract_budget(
            parsed,
            budget_cap=budget_cap,
            review_threshold=review_threshold,
            review_acknowledged=review_acknowledged,
        )
        view = self._execute_contract(parsed, compiled)
        if confirm_id:
            self._confirmed[confirm_id] = view
        return view

    def approve_assembly(
        self,
        pending_id: str,
        *,
        session: Session,
        approved: bool = True,
        required_assurance: int = 1,
    ) -> AssemblyRunView:
        """Decide a held reserved contract — approval mints from identity.

        Like :meth:`approve`, the decision comes from a verified identity
        session, never from caller text: an unauthorized session raises and
        the contract stays held. Approval re-runs the budget gate (prices
        may have moved while it waited) and then executes through the same
        shared money path; declining removes it. Both outcomes are audited
        with the decider's principal. Holds are durable: a hold made before
        a shell restart is still here to decide (the contract recompiles
        once in the deciding process) — unless it expired first, in which
        case it was swept (audited) and the decision is a KeyError -> 404.
        """
        self._sweep_holds()
        entry = self._pending.get(pending_id)
        if entry is None:
            raise KeyError(pending_id)
        if not approved:
            self._pending.remove(pending_id)
            self._compiled_holds.pop(pending_id, None)
            self._durable.audit.append(
                "contract.declined",
                {"pending_id": pending_id, "by": session.principal_id},
            )
            return AssemblyRunView(run_id=pending_id, status="declined")
        if self._approval is None:
            raise RuntimeError("no approval authority configured")
        parsed, compiled = self._compiled_for(entry)
        record = self._approval.approve(
            session,
            run_id=pending_id,
            policy=parsed.name,
            requester_id="desktop",
            required_assurance=required_assurance,
        )
        self._enforce_contract_budget(
            parsed,
            budget_cap=entry.budget_cap,
            review_threshold=entry.review_threshold,
            review_acknowledged=entry.review_acknowledged,
        )
        view = self._execute_contract(parsed, compiled)
        self._pending.remove(pending_id)
        self._compiled_holds.pop(pending_id, None)
        self._durable.audit.append(
            "contract.approved",
            {
                "pending_id": pending_id,
                "run_id": view.run_id,
                "approval_id": record.id,
                "by": session.principal_id,
                "reserved": entry.reserved,
            },
        )
        return view

    def _sweep_holds(self) -> None:
        """Lazily expire stale holds; every sweep is audited per hold."""
        for record in self._pending.sweep_expired(self._clock()):
            self._compiled_holds.pop(record.pending_id, None)
            self._durable.audit.append(
                "contract.expired",
                {"pending_id": record.pending_id, "reserved": record.reserved},
            )

    def _compiled_for(self, entry: PendingContractRecord):
        """The hold's runnable form — cached, or recompiled after a restart."""
        cached = self._compiled_holds.get(entry.pending_id)
        if cached is not None:
            return cached
        from ..nodeplace.execution import compile_contract
        from ..skills.contract import NodeContract

        parsed = NodeContract.model_validate(entry.contract)
        compiled = compile_contract(parsed)
        self._compiled_holds[entry.pending_id] = (parsed, compiled)
        return parsed, compiled

    def decide_assembly(
        self,
        pending_id: str,
        *,
        token: str,
        approved: bool,
        required_assurance: int = 1,
    ) -> AssemblyRunView:
        """The loopback's approval decision: a bearer token, verified here.

        The loopback boundary has no auth of its own, so the decision
        carries an identity token and this method turns it into a verified
        :class:`Session` (``AuthenticationError`` if it cannot) before
        handing off to :meth:`approve_assembly` — the caller's text never
        becomes authority.
        """
        if self._sessions is None:
            raise KeyError("no session manager configured for this shell")
        session = self._sessions.login(token, now=datetime.now(UTC))
        return self.approve_assembly(
            pending_id,
            session=session,
            approved=approved,
            required_assurance=required_assurance,
        )

    def _enforce_contract_budget(
        self,
        parsed,
        *,
        budget_cap: float | None,
        review_threshold: float | None,
        review_acknowledged: bool,
    ) -> None:
        # Budget gate BEFORE anything commits: a cap refuses outright;
        # review reasons (threshold, spending behavior, a linked wallet
        # that may only be partial) block until explicitly acknowledged.
        # Both raise PermissionError subclasses -> 403 at the loopback.
        from ..nodeplace.budget import BudgetPolicy, assess_budget, enforce_budget
        from ..nodeplace.execution import estimate_contract_gross

        estimate = estimate_contract_gross(
            parsed, assembler=self._market, price_book=self._price_book
        )
        enforce_budget(
            assess_budget(
                estimate.gross,
                policy=BudgetPolicy(
                    hard_cap=budget_cap, review_threshold=review_threshold
                ),
                spend_history=self._spend_history(),
                class_history=(
                    self._spend_history(estimate.goal_class)
                    if estimate.goal_class is not None
                    else None
                ),
                goal_class=estimate.goal_class,
                wallet_balance=self._wallet_balance(),
            ),
            review_acknowledged=review_acknowledged,
        )

    def _execute_contract(self, parsed, compiled) -> AssemblyRunView:
        from ..nodeplace.execution import execute_contract

        result = execute_contract(
            parsed,
            compiled,
            runner=self._contract_runner,
            assembler=self._market,
            price_book=self._price_book,
            attribution=self._attribution,
            audit=self._durable.audit,
            consumer_tenant="local",
            consumer_principal="desktop",
            trace_store=self._trace_store,
        )
        return AssemblyRunView(
            run_id=result.run_id,
            status=result.status,
            error=result.error,
            steps=[
                AssemblyRunStepView(status=o["status"], error=o["error"])
                for o in result.outcomes
            ],
            gross=result.market.gross,
            provider_cost=result.market.provider_cost,
            noders=list(result.market.noders),
        )

    # ------------------------------------------------------------------ #
    # Worker health + trusted/untrusted execution labeling.              #
    # ------------------------------------------------------------------ #
    def worker_health(self) -> WorkerHealthView:
        labels = []
        for trust in (TrustLevel.UNTRUSTED_SYNTHESIZED, TrustLevel.TRUSTED_LOCAL_SKILL):
            allowed = sorted(self._isolation.allowed_backends(trust))
            isolated = "subprocess" not in allowed
            labels.append(
                ExecutionLabel(
                    trust_level=trust.value,
                    allowed_backends=allowed,
                    isolated=isolated,
                    label=(
                        "Untrusted — isolated container only"
                        if trust is TrustLevel.UNTRUSTED_SYNTHESIZED
                        else "Trusted local skill — may run on host"
                    ),
                )
            )
        return WorkerHealthView(docker_available=self._docker_available, labels=labels)

    # ------------------------------------------------------------------ #
    # Offline policy + local data export / deletion.                      #
    # ------------------------------------------------------------------ #
    def offline_policy(self) -> dict[str, str]:
        return {
            "network": "local-only",
            "telemetry": "disabled",
            "data_location": "local SQLite + filesystem",
            "export": "supported",
            "deletion": "supported",
        }

    def export_data(self, run_id: str) -> ExportBundle:
        state = self._require(run_id)
        history = self._durable.reconstruct_history(run_id)
        return ExportBundle(
            run_id=run_id,
            generated_at=datetime.now(UTC),
            run_state=state.model_dump(mode="json"),
            approvals=[a.model_dump(mode="json") for a in history["approvals"]],
            incidents=[i.model_dump(mode="json") for i in history["incidents"]],
            audit=[
                AuditEntryView(seq=r.seq, event_type=r.event_type, at=r.at)
                for r in history["audit"]
            ],
        )

    def delete_data(self, run_id: str) -> dict[str, int]:
        return delete_workflow(self._durable.conn, run_id)

    # ------------------------------------------------------------------ #
    # Internals.                                                          #
    # ------------------------------------------------------------------ #
    def _require(self, run_id: str) -> RunState:
        state = self._durable.get(run_id)
        if state is None:
            raise KeyError(f"unknown run: {run_id}")
        return state

    def _task_view(self, state: RunState) -> TaskView:
        questions: list[QuestionView] = []
        if (
            state.pause is not None
            and state.pause.kind is PauseKind.CLARIFICATION
            and state.compilation is not None
        ):
            questions = [
                QuestionView(
                    parameter=q.parameter,
                    question=q.question,
                    suggested_values=list(q.suggested_values),
                    priority=q.priority,
                )
                for q in state.compilation.questions
            ]
        return TaskView(
            run_id=state.run_id,
            intent=state.intent,
            phase=state.phase.value,
            awaiting=_PAUSE_LABELS[state.pause.kind] if state.pause else None,
            prompt=state.pause.prompt if state.pause else None,
            questions=questions,
            can_cancel=not state.is_terminal,
            failure_reason=state.failure_reason,
            result=state.result,
        )

    def _blueprint_view(self, route: RoutePlan) -> BlueprintView:
        return self._blueprint_view_of(route.chosen)

    @staticmethod
    def _blueprint_view_of(blueprint) -> BlueprintView:
        return BlueprintView(
            id=blueprint.id,
            name=blueprint.name,
            estimated_cost=blueprint.estimated_cost,
            excluded=blueprint.excluded,
            exclusion_reason=blueprint.exclusion_reason,
            actions=[
                ActionView(
                    adapter=item.action.adapter,
                    operation=item.action.operation,
                    reserved=item.reserved,
                    risk=item.risk,
                )
                for item in blueprint.actions
            ],
            reserved_action_count=sum(1 for item in blueprint.actions if item.reserved),
        )

    def _connection_view(self, connection: _Connection) -> ProviderConnectionView:
        return ProviderConnectionView(
            connection_id=connection.connection_id,
            provider=connection.provider,
            status=connection.status,
            scopes=list(connection.scopes),
            connected_at=connection.connected_at,
            credential_ref_id=connection.credential_ref.ref_id,
        )
