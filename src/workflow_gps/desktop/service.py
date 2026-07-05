"""The desktop shell's application service — the local loopback boundary.

A desktop UI binds to this service (over a loopback API or named pipe); it never
touches the orchestrator, durable stores, or vault directly. Every workflow action
goes through the backend's own gates: clarification/confirmation/approval/incident
are driven via the durable service's resume path (so the orchestrator's execution
preflight still applies), approvals are minted only from an authorized identity
session, and provider secrets live in the vault and never appear in a view.

There is deliberately **no execute method here** — the shell cannot run code or
bypass policy; it can only request backend operations and present their results.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ..durable.maintenance import delete_workflow
from ..durable.service import DurableWorkflowService
from ..identity.models import Session
from ..identity.service import IdentityApprovalAuthority
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
    ):
        self._durable = durable
        self._approval = approval_authority
        self._vault = vault or SecretVault()
        self._isolation = isolation or IsolationPolicy()
        self._docker_available = docker_available
        self._market = market
        self._price_book = price_book
        self._connections: dict[str, _Connection] = {}

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
        from ..orchestrator.assembler import GoalSpec

        spec = GoalSpec.model_validate({"name": goal, "want": want, "have": have or []})
        preview = preview_assembly(
            self._market, self._price_book, spec, query=query, fill_gaps=fill_gaps
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
