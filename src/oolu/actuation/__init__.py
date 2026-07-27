"""The robotic workflow actuation protocol — contracts only, no commands.

This package is stage R0 of docs/robotic-actuation-protocol-plan.md: the
typed foundation for approval-gated physical actuation. It is a pure
library — no I/O, no transport, and deliberately no surface through which
a model could emit a servo or drive command. What it pins down:

* canonical digests that bind every execution-critical field of a job
  (``digests``), so substitution of a tool, program, frame, or approval
  is a digest mismatch rather than a policy violation;
* the typed contracts themselves (``contracts``): intents, capabilities,
  validated operating envelopes, workcells, digest-bound jobs, and
  envelope-bounded parameter proposals;
* single-use, nonce-bound execution leases (``lease``);
* the actuation lifecycle state machine (``lifecycle``), where a safety
  stop never resumes and arming is a composed gate;
* consequence-keyed human review tiers A0–A5 (``review``);
* live preflight verdicts (``preflight``) — any failure blocks arming
  with a named reason.
"""

from .contracts import (
    ActionIntent,
    ActuationCapability,
    ActuationJob,
    CapabilityFamily,
    Disposition,
    ExecutionEvidenceBundle,
    ParameterProposal,
    RecipeVersion,
    Reversibility,
    ToolInstance,
    ValidatedOperatingEnvelope,
    WorkcellVersion,
)
from .digests import aggregate_digest, canonical_json, sha256_hex
from .lease import ExecutionLease, LeaseRefusal, LeaseRegistry
from .lifecycle import ActuationState, ArmingDecision, arm, can_transition, transition
from .preflight import LiveWorkcellState, PreflightResult, run_preflight
from .review import ReviewDecision, ReviewSignals, ReviewTier, assign_tier

__all__ = [
    "ActionIntent",
    "ActuationCapability",
    "ActuationJob",
    "ActuationState",
    "ArmingDecision",
    "CapabilityFamily",
    "Disposition",
    "ExecutionEvidenceBundle",
    "ExecutionLease",
    "LeaseRefusal",
    "LeaseRegistry",
    "LiveWorkcellState",
    "ParameterProposal",
    "PreflightResult",
    "RecipeVersion",
    "ReviewDecision",
    "ReviewSignals",
    "ReviewTier",
    "Reversibility",
    "ToolInstance",
    "ValidatedOperatingEnvelope",
    "WorkcellVersion",
    "aggregate_digest",
    "arm",
    "assign_tier",
    "can_transition",
    "canonical_json",
    "run_preflight",
    "sha256_hex",
    "transition",
]
