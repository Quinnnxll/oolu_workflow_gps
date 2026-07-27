"""Simulation-only routing — stage R3 of the actuation protocol.

A physical goal becomes a ranked set of compatible capability nodes, a
digest-bound plan, a qualified simulation record, and an approval
bundle — and nothing more. No module in this package can emit a machine
command; the terminal artifact is a plan release *eligible* for R4's
deterministic compiler, and the router's front door rejects raw
servo-or-drive vocabulary by name before anything is scored.

The pieces:

* ``nodes``: measurement and actuation capability nodes — one contract
  for robots and specialized machines alike, differing only in declared
  effects, constraints, and hazards;
* ``router``: the typed-intent wall, hard exclusions with named
  reasons, and the §18.1 additive score with its full breakdown;
* ``simulation``: simulator qualification records (a simulator is
  itself equipment with a validated scope) and simulation records that
  must name the exact workcell, tool, fixture, frame, recipe, and
  configuration versions they simulated;
* ``planner``: plan assembly under envelope containment, with
  disposition paths mandatory for irreversible effects, and the
  release gate that checks plan, simulation, review, and approvals
  together;
* ``approvals``: durable, digest-bound approval bundles speaking the
  house law — approval of digest A can never release digest B, each
  approval spends exactly once, and prohibited tiers cannot even open
  a bundle.
"""

from .approvals import ActuationApprovalService, ApprovalStatus
from .nodes import ActuationNode, MeasurementNode
from .planner import ActuationPlan, build_plan, ready_for_compilation
from .router import (
    RawCommandRejected,
    RoutingDecision,
    ScoreBreakdown,
    ScoredCandidate,
    route,
)
from .simulation import (
    SimulationRecord,
    SimulatorQualification,
    qualify_simulation,
)

__all__ = [
    "ActuationApprovalService",
    "ActuationNode",
    "ActuationPlan",
    "ApprovalStatus",
    "MeasurementNode",
    "RawCommandRejected",
    "RoutingDecision",
    "ScoreBreakdown",
    "ScoredCandidate",
    "SimulationRecord",
    "SimulatorQualification",
    "build_plan",
    "qualify_simulation",
    "ready_for_compilation",
    "route",
]
