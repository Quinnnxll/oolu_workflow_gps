"""The commercial spine, assembled: intents in, deterministic law, digests out.

``MarketplaceSpine`` is M0 of docs/marketplace-build-plan.md as one service:
it mints immutable commercial intents (idempotently), evaluates them through
the pure policy ladder, files review-required intents for digest-bound
approval, and — for M1's order service to consume — authorizes execution
only when the LIVE terms still hash to the digest a human approved and the
agent's delegation is still alive.

Two things are structural, and tests pin both:

- **Every decision lands on the audit chain**, including denials, expiries,
  blocks, and digest mismatches. The chain is the provenance the spec
  demands; nothing here happens off it.
- **No money.** This module imports no billing, no payment, no provider
  code, and produces at most an :class:`ExecutionAuthorization` — a typed
  fact, not a transfer. The phase's exit gate is that absence.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .delegation import AgentDelegation, DelegationStore, delegation_gaps
from .digest import intent_digest
from .errors import (
    ApprovalConsumed,
    ApprovalExpired,
    DelegationBlocked,
    DigestMismatch,
    DuplicateApprover,
    IntentExpired,
    MarketNotFound,
    StrongAuthenticationRequired,
    WrongState,
)
from .models import (
    ApprovalRecord,
    CommercialIntent,
    ExecutionAuthorization,
    IntentAction,
    Offer,
)
from .policy import Decision, PurchaseFacts, PurchasePolicy, evaluate_purchase
from .review import approval_summary
from .store import ApprovalStore, IntentStore, PolicyStore, StoredIntent

_STATE_BY_DECISION = {
    Decision.AUTO_EXECUTE: "auto_approved",
    Decision.EXECUTE_AND_NOTIFY: "notify",
    Decision.REQUIRE_APPROVAL: "approval_pending",
    Decision.REQUIRE_STRONG_APPROVAL: "approval_pending",
    Decision.REQUIRE_MULTIPLE_APPROVERS: "approval_pending",
    Decision.DENY: "denied",
}


class MarketplaceSpine:
    """Intents, policy, approvals, delegation — one seam, zero payments."""

    def __init__(self, conn, *, audit, history=None) -> None:
        self.intents = IntentStore(conn)
        self.approvals = ApprovalStore(conn)
        self.policies = PolicyStore(conn)
        self.delegations = DelegationStore(conn)
        self._audit = audit
        # M2: the order-history port (fraud.OrderHistory) that DERIVES the
        # risk facts M0 took on faith — spend totals, counterparty
        # familiarity, reputation, and the deterministic risk signals. A
        # fact the caller supplies explicitly still wins; the port only
        # fills silence, so the ladder stays a pure function of its
        # typed inputs either way.
        self._history = history

    # ------------------------------------------------------------------ #
    # Policy — the principal's stored, versioned limits.                  #
    # ------------------------------------------------------------------ #
    def purchase_policy(self, *, tenant: str, principal: str) -> PurchasePolicy:
        return self.policies.get(tenant=tenant, principal=principal)

    def set_purchase_policy(
        self, policy: PurchasePolicy, *, tenant: str, principal: str
    ) -> PurchasePolicy:
        self.policies.put(policy, tenant=tenant, principal=principal)
        self._audit.append(
            "market.policy.updated",
            {
                "tenant": tenant,
                "principal": principal,
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
            },
        )
        return policy

    # ------------------------------------------------------------------ #
    # Delegation — the agent's whole authority.                           #
    # ------------------------------------------------------------------ #
    def grant_delegation(self, record: AgentDelegation) -> AgentDelegation:
        self.delegations.grant(record)
        self._audit.append(
            "market.delegation.granted",
            {
                "tenant": record.tenant_id,
                "principal": record.principal_id,
                "delegation_id": record.delegation_id,
                "agent": record.agent_id,
                "allowed_actions": list(record.allowed_actions),
                "expires_at": record.expires_at.isoformat(),
            },
        )
        return record

    def revoke_delegation(
        self, delegation_id: str, *, tenant: str, principal: str, now: datetime
    ) -> list[str]:
        """Revoke, then block every unexecuted intent the agent minted —
        the spec's acceptance test, enforced at the moment of revocation
        (and re-checked live at every authorization)."""
        record = self.delegations.get(delegation_id, tenant=tenant)
        if record is None or record.principal_id != principal:
            raise MarketNotFound("no such delegation")
        self.delegations.revoke(delegation_id, tenant=tenant, now=now)
        blocked = self.intents.block_for_agent(
            tenant=tenant, principal=principal, agent=record.agent_id
        )
        self._audit.append(
            "market.delegation.revoked",
            {
                "tenant": tenant,
                "principal": principal,
                "delegation_id": delegation_id,
                "agent": record.agent_id,
                "blocked_intents": blocked,
            },
        )
        return blocked

    # ------------------------------------------------------------------ #
    # Intents — immutable asks through the deterministic ladder.          #
    # ------------------------------------------------------------------ #
    def create_purchase_intent(
        self,
        *,
        tenant: str,
        principal: str,
        agent: str,
        offer: Offer,
        idempotency_key: str,
        now: datetime,
        category: str = "",
        delivery_destination: str = "",
        data_permissions: tuple[str, ...] = (),
        maximum_total_micros: int | None = None,
        risk_facts: dict[str, Any] | None = None,
        expires_in_minutes: int = 24 * 60,
    ) -> tuple[StoredIntent, bool]:
        """Mint (or return) the intent for this idempotency key, evaluated.

        Offer-bound facts — amount, currency, counterparty, refundability,
        recurrence — are DERIVED from the offer and cannot be supplied by
        the caller: the policy engine judges the terms as they are, not as
        described. ``risk_facts`` carries only the risk signals (benchmark,
        first-time counterparty, fraud/anomaly scores, …) that later phases
        will derive from history and providers.
        """
        risk = dict(risk_facts or {})
        unknown = set(risk) - set(PurchaseFacts.model_fields)
        if unknown:
            raise ValueError(f"unknown risk facts: {sorted(unknown)}")
        if self._history is not None:
            risk = self._derive_history_facts(
                risk,
                tenant=tenant,
                principal=principal,
                counterparty=offer.seller_id,
                amount_micros=offer.total_micros,
                now=now,
            )
        maximum = (
            offer.total_micros if maximum_total_micros is None else maximum_total_micros
        )
        policy = self.purchase_policy(tenant=tenant, principal=principal)
        delegation = self.delegations.active_for(
            tenant=tenant, principal=principal, agent=agent, now=now
        )
        gaps = delegation_gaps(
            delegation,
            action=IntentAction.PURCHASE.value,
            category=category,
            amount_micros=offer.total_micros,
            counterparty=offer.seller_id,
            destination=delivery_destination,
            now=now,
        )
        derived = dict(
            amount_micros=offer.total_micros,
            currency=offer.currency,
            category=category,
            counterparty=offer.seller_id,
            refundable=offer.refundable,
            recurring=bool(offer.recurring_terms),
            delivery_destination=delivery_destination,
            agent_permission_gaps=gaps,
            budget_exceeded=offer.total_micros > maximum,
        )
        facts = PurchaseFacts(**{**risk, **derived})
        intent = CommercialIntent(
            intent_id=uuid4().hex,
            tenant_id=tenant,
            principal_id=principal,
            agent_id=agent,
            action=IntentAction.PURCHASE,
            offer_snapshot=offer,
            category=category,
            delivery_destination=delivery_destination,
            data_permissions=tuple(data_permissions),
            maximum_total_micros=maximum,
            approval_policy_id=policy.policy_id,
            idempotency_key=idempotency_key,
            created_at=now,
            expires_at=now + timedelta(minutes=expires_in_minutes),
        )
        verdict = evaluate_purchase(policy, facts)
        digest = intent_digest(intent, policy_version=verdict.policy_version)
        stored, created = self.intents.add(
            intent,
            state=_STATE_BY_DECISION[verdict.decision],
            digest=digest,
            verdict=verdict,
        )
        if not created:
            return stored, False
        self._audit.append(
            "market.intent.created",
            {
                "tenant": tenant,
                "principal": principal,
                "agent": agent,
                "intent_id": intent.intent_id,
                "action": intent.action.value,
                "offer_id": offer.offer_id,
                "offer_version": offer.offer_version,
                "total_micros": offer.total_micros,
                "currency": offer.currency,
                "idempotency_key": idempotency_key,
            },
        )
        self._audit.append(
            "market.policy.decision",
            {
                "tenant": tenant,
                "intent_id": intent.intent_id,
                "decision": verdict.decision.value,
                "reasons": list(verdict.reasons),
                "policy_version": verdict.policy_version,
                "required_approvers": verdict.required_approvers,
                "intent_digest": digest,
            },
        )
        return stored, True

    def _derive_history_facts(
        self,
        risk: dict[str, Any],
        *,
        tenant: str,
        principal: str,
        counterparty: str,
        amount_micros: int,
        now: datetime,
    ) -> dict[str, Any]:
        """Fill unspoken risk facts from the order book. Explicit facts
        stand; every derivation is audited so the verdict's inputs stay
        reconstructable."""
        derived: dict[str, Any] = {}
        if "daily_total_micros" not in risk or "monthly_total_micros" not in risk:
            daily, monthly = self._history.spend_totals(
                tenant=tenant, principal=principal, now=now
            )
            derived.setdefault("daily_total_micros", daily)
            derived.setdefault("monthly_total_micros", monthly)
        if "first_time_counterparty" not in risk:
            derived["first_time_counterparty"] = not self._history.seen_counterparty(
                tenant=tenant, principal=principal, counterparty=counterparty
            )
        if "counterparty_reputation" not in risk:
            reputation = self._history.reputation(counterparty=counterparty)
            if reputation is not None:
                derived["counterparty_reputation"] = reputation
        if "fraud_score" not in risk and "anomaly_score" not in risk:
            signals = self._history.assess(
                tenant=tenant,
                principal=principal,
                counterparty=counterparty,
                amount_micros=amount_micros,
                price_benchmark_micros=risk.get("price_benchmark_micros"),
                now=now,
            )
            derived["fraud_score"] = signals.fraud_score
            derived["anomaly_score"] = signals.anomaly_score
            if signals.reasons:
                self._audit.append(
                    "market.risk.assessed",
                    {
                        "tenant": tenant,
                        "principal": principal,
                        "counterparty": counterparty,
                        "fraud_score": signals.fraud_score,
                        "anomaly_score": signals.anomaly_score,
                        "reasons": list(signals.reasons),
                    },
                )
        merged = dict(derived)
        merged.update(risk)  # the caller's explicit facts always win
        return merged

    def get_intent(self, intent_id: str, *, tenant: str) -> StoredIntent:
        stored = self.intents.get(intent_id, tenant=tenant)
        if stored is None:
            raise MarketNotFound("no such intent")
        return stored

    def list_intents(
        self, *, tenant: str, principal: str | None = None, state: str | None = None
    ) -> list[StoredIntent]:
        return self.intents.list_for(tenant=tenant, principal=principal, state=state)

    # ------------------------------------------------------------------ #
    # Review — approvals bound to the digest, once, with the right proof. #
    # ------------------------------------------------------------------ #
    def pending_approvals(
        self, *, tenant: str, principal: str | None = None, now: datetime
    ) -> list[dict]:
        """The review inbox: exact-terms summaries of every waiting intent.
        Expiry is swept lazily here — a stale ask never rots in the queue."""
        summaries: list[dict] = []
        for stored in self.intents.list_for(
            tenant=tenant, principal=principal, state="approval_pending"
        ):
            if stored.intent.expires_at <= now:
                self._expire(stored, tenant=tenant)
                continue
            summaries.append(
                approval_summary(stored.intent, stored.verdict, digest=stored.digest)
            )
        return summaries

    def record_approval(
        self,
        intent_id: str,
        *,
        tenant: str,
        approver_id: str,
        assurance_level: int,
        approve: bool,
        now: datetime,
    ) -> ApprovalRecord:
        stored = self.get_intent(intent_id, tenant=tenant)
        if stored.state != "approval_pending":
            raise WrongState(f"intent is {stored.state}, not awaiting approval")
        if stored.intent.expires_at <= now:
            self._expire(stored, tenant=tenant)
            raise IntentExpired("the intent expired before the decision")
        strength = "strong" if assurance_level >= 2 else "normal"
        # The floor guards COMMITMENT: approving needs the verdict's
        # strength; declining is always safe at any assurance.
        if (
            approve
            and stored.verdict.approval_strength == "strong"
            and strength != "strong"
        ):
            raise StrongAuthenticationRequired(
                "approving this intent requires step-up authentication"
            )
        prior = [
            record
            for record, _ in self.approvals.for_intent(intent_id, tenant=tenant)
            if record.decision == "approved"
        ]
        if any(record.approver_id == approver_id for record in prior):
            raise DuplicateApprover("this approver has already decided")
        record = ApprovalRecord(
            approval_id=uuid4().hex,
            tenant_id=tenant,
            intent_id=intent_id,
            intent_digest=stored.digest,
            approver_id=approver_id,
            authentication_strength=strength,
            decision="approved" if approve else "rejected",
            created_at=now,
            expires_at=now + timedelta(minutes=stored.verdict.approval_ttl_minutes),
        )
        self.approvals.add(record)
        self._audit.append(
            "market.approval.recorded",
            {
                "tenant": tenant,
                "intent_id": intent_id,
                "approval_id": record.approval_id,
                "approver": approver_id,
                "decision": record.decision,
                "authentication_strength": strength,
                "intent_digest": stored.digest,
                "approvals_recorded": len(prior) + (1 if approve else 0),
                "approvals_required": stored.verdict.required_approvers,
            },
        )
        if not approve:
            self.intents.set_state(
                intent_id, tenant=tenant, state="rejected",
                expected=("approval_pending",),
            )
        elif len(prior) + 1 >= stored.verdict.required_approvers:
            self.intents.set_state(
                intent_id, tenant=tenant, state="approved",
                expected=("approval_pending",),
            )
        return record

    # ------------------------------------------------------------------ #
    # Authorization — the digest law, enforced. Still no money.           #
    # ------------------------------------------------------------------ #
    def authorize_execution(
        self,
        intent_id: str,
        *,
        tenant: str,
        current_offer: Offer,
        now: datetime,
    ) -> ExecutionAuthorization:
        """Authorize the EXACT approved terms against the LIVE offer.

        This is the seam M1's order service calls before any payment
        authorization. It consumes approvals (single-use), re-checks the
        delegation (revocation blocks immediately), and refuses any digest
        drift. It moves no money — it cannot; nothing here knows how.
        """
        stored = self.get_intent(intent_id, tenant=tenant)
        intent = stored.intent
        if stored.state == "authorized":
            raise WrongState("the intent is already authorized")
        if stored.state not in ("auto_approved", "notify", "approved"):
            raise WrongState(f"intent is {stored.state}, not executable")
        if intent.expires_at <= now:
            self._expire(stored, tenant=tenant)
            raise IntentExpired("the intent expired before authorization")
        delegation = self.delegations.active_for(
            tenant=tenant,
            principal=intent.principal_id,
            agent=intent.agent_id,
            now=now,
        )
        if delegation is None:
            self.intents.set_state(
                intent_id, tenant=tenant, state="blocked",
                expected=("auto_approved", "notify", "approved"),
            )
            self._audit.append(
                "market.intent.blocked",
                {
                    "tenant": tenant,
                    "intent_id": intent_id,
                    "reason": "the agent's delegation is revoked or expired",
                },
            )
            raise DelegationBlocked(
                "the agent's delegation is no longer live; the intent is blocked"
            )
        live_digest = intent_digest(
            intent, offer=current_offer, policy_version=stored.verdict.policy_version
        )
        if live_digest != stored.digest:
            self._audit.append(
                "market.digest.mismatch",
                {
                    "tenant": tenant,
                    "intent_id": intent_id,
                    "approved_digest": stored.digest,
                    "live_digest": live_digest,
                    "approved_offer_version": intent.offer_snapshot.offer_version,
                    "live_offer_version": current_offer.offer_version,
                },
            )
            raise DigestMismatch(
                "the live terms no longer match the approved digest — "
                "a new intent and approval cycle is required"
            )
        approval_ids: tuple[str, ...] = ()
        if stored.state == "approved":
            approval_ids = self._consume_approvals(stored, tenant=tenant, now=now)
        moved = self.intents.set_state(
            intent_id, tenant=tenant, state="authorized",
            expected=("auto_approved", "notify", "approved"),
        )
        if not moved:
            raise WrongState("the intent moved concurrently; nothing authorized")
        authorization = ExecutionAuthorization(
            authorization_id=uuid4().hex,
            tenant_id=tenant,
            intent_id=intent_id,
            intent_digest=stored.digest,
            approval_ids=approval_ids,
            authorized_at=now,
        )
        self._audit.append(
            "market.execution.authorized",
            {
                "tenant": tenant,
                "intent_id": intent_id,
                "authorization_id": authorization.authorization_id,
                "intent_digest": stored.digest,
                "approval_ids": list(approval_ids),
                "decision": stored.verdict.decision.value,
            },
        )
        return authorization

    def _consume_approvals(
        self, stored: StoredIntent, *, tenant: str, now: datetime
    ) -> tuple[str, ...]:
        """Spend enough live, digest-bound approvals — each exactly once."""
        usable: list[ApprovalRecord] = []
        for record, consumed in self.approvals.for_intent(
            stored.intent.intent_id, tenant=tenant
        ):
            if record.decision != "approved":
                continue
            if record.intent_digest != stored.digest:
                continue
            if consumed:
                raise ApprovalConsumed("an approval was already consumed")
            if record.expires_at <= now:
                raise ApprovalExpired(
                    "the approval expired; a fresh review is required"
                )
            usable.append(record)
        needed = stored.verdict.required_approvers
        if len(usable) < needed:
            raise WrongState("not enough live approvals to authorize")
        spending = usable[:needed]
        for record in spending:
            if not self.approvals.consume(record.approval_id, tenant=tenant, now=now):
                raise ApprovalConsumed("an approval was consumed concurrently")
        return tuple(record.approval_id for record in spending)

    def _expire(self, stored: StoredIntent, *, tenant: str) -> None:
        moved = self.intents.set_state(
            stored.intent.intent_id,
            tenant=tenant,
            state="expired",
            expected=("approval_pending", "auto_approved", "notify", "approved"),
        )
        if moved:
            self._audit.append(
                "market.intent.expired",
                {"tenant": tenant, "intent_id": stored.intent.intent_id},
            )
