"""The actuation lifecycle — §24.5's state machine, with arming as a gate.

Two properties are load-bearing and tested:

* **No resume after a safety stop.** ``SAFE_STOPPED`` transitions only
  to ``CONTAINED``; the way back to physical execution is a *new*
  proposal through every gate (acceptance test 31 — no automatic
  restart after a safety trip, no silent parameter retry).
* **Arming is composed, not asserted.** ``arm`` refuses unless the
  recomputed aggregate digest matches the approved one, live preflight
  passed with zero failures, and the single-use lease consumes cleanly
  — each refusal carries its named reason.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from .contracts import ActuationJob
from .lease import LeaseRegistry
from .preflight import PreflightResult


class ActuationState(str, Enum):
    PROPOSED = "proposed"
    SIMULATED = "simulated"
    REVIEWED = "reviewed"
    COMPILED = "compiled"
    PREFLIGHT = "preflight"
    ARMED = "armed"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    ACCEPTED = "accepted"
    REWORK = "rework"
    SAFE_STOPPED = "safe_stopped"
    CONTAINED = "contained"


_ALLOWED: dict[ActuationState, frozenset[ActuationState]] = {
    ActuationState.PROPOSED: frozenset({ActuationState.SIMULATED}),
    ActuationState.SIMULATED: frozenset({ActuationState.REVIEWED}),
    ActuationState.REVIEWED: frozenset({ActuationState.COMPILED}),
    ActuationState.COMPILED: frozenset({ActuationState.PREFLIGHT}),
    ActuationState.PREFLIGHT: frozenset({ActuationState.ARMED}),
    ActuationState.ARMED: frozenset({ActuationState.EXECUTING}),
    ActuationState.EXECUTING: frozenset(
        {ActuationState.VERIFYING, ActuationState.SAFE_STOPPED}
    ),
    ActuationState.VERIFYING: frozenset(
        {ActuationState.ACCEPTED, ActuationState.REWORK}
    ),
    # Rework re-enters at PROPOSED: a changed job is a new approval cycle.
    ActuationState.REWORK: frozenset({ActuationState.PROPOSED}),
    # A safety stop only ever settles into containment. Never back to
    # EXECUTING, ARMED, or PREFLIGHT — recovery is a new proposal.
    ActuationState.SAFE_STOPPED: frozenset({ActuationState.CONTAINED}),
    ActuationState.ACCEPTED: frozenset(),
    ActuationState.CONTAINED: frozenset(),
}


class TransitionError(ValueError):
    pass


def can_transition(current: ActuationState, target: ActuationState) -> bool:
    return target in _ALLOWED[current]


def transition(current: ActuationState, target: ActuationState) -> ActuationState:
    if not can_transition(current, target):
        raise TransitionError(f"illegal transition {current.value} -> {target.value}")
    return target


class ArmingDecision(BaseModel):
    """The outcome of the arming gate: armed, or refused by name."""

    model_config = ConfigDict(frozen=True)

    armed: bool
    refusals: tuple[str, ...] = ()


def arm(
    job: ActuationJob,
    *,
    approved_digest: str,
    preflight: PreflightResult,
    leases: LeaseRegistry,
    lease_id: str,
    now: int,
) -> ArmingDecision:
    """Compose the arming gate. Every check runs; refusals accumulate.

    The lease is consumed only when digest and preflight already hold,
    so a mismatch never spends the single use.
    """

    refusals: list[str] = []
    live_digest = job.aggregate()
    if live_digest != approved_digest:
        refusals.append("aggregate_digest_mismatch")
    if not preflight.passed:
        refusals.extend(f"preflight:{failure}" for failure in preflight.failures)
    if refusals:
        return ArmingDecision(armed=False, refusals=tuple(refusals))
    refusal = leases.consume(
        lease_id=lease_id, job_digest=live_digest, nonce=job.nonce, now=now
    )
    if refusal is not None:
        return ArmingDecision(armed=False, refusals=(f"lease:{refusal.value}",))
    return ArmingDecision(armed=True)
