"""Verified feedback and the science stack — stage R5, the loop closed.

Physical execution evidence becomes qualified state deltas, state
deltas and observations become rights-checked datasets, datasets feed a
formula sandbox whose candidates can never promote themselves, and an
approved world model proposes the next experiment — which re-enters the
protocol at the front door as an unauthorized intent, never as a
command. What each module pins:

* ``state_delta``: a physical state change is qualified only by
  independent inspection, its lineage runs digest-deep to the job, and
  an irreversible failure routes to a *declared* disposition path —
  "rollback" is not a word this vocabulary contains;
* ``safety_validation``: simulation success alone can never satisfy a
  physical safety-function validation gate;
* ``capability_learning``: process capability learns from every
  outcome — rejected parts, near misses, and trips included — under
  explicit rights, and negatives lower the routing signal instead of
  disappearing;
* ``dataset``: deterministic, rights-checked dataset builds with named
  exclusions, replication splits across facilities, and shared
  calibration dependencies detected before independence is claimed;
* ``formulas``: the discovery sandbox — dimensional invalidity is
  automatic rejection, failed candidates stay stored, and no path in
  the candidate registry reaches "approved";
* ``worldmodel``: promotion demands independent replication and three
  distinct reviewers (never the discovery node itself); approved
  versions are immutable, challenged versions remain reproducible;
* ``experiment``: the §23 priority arithmetic proposes the next test
  as a typed intent with no authorization attached — new evidence
  yields a new candidate draft, never a silent model mutation.
"""

from .capability_learning import OutcomeKind, ProcessCapabilityLedger, ProcessOutcome
from .dataset import (
    CandidateObservation,
    DatasetDefinition,
    DatasetManifest,
    build_dataset,
)
from .experiment import ExperimentProposal, propose_experiment, update_with_evidence
from .formulas import (
    Dimension,
    FormulaCandidate,
    FormulaCandidateRegistry,
    Term,
)
from .safety_validation import ValidationEvidence, validate_safety_function
from .state_delta import (
    InspectionFinding,
    QualifiedStateDelta,
    StateDeltaRefused,
    qualify_state_delta,
    route_failure,
)
from .worldmodel import (
    PromotionRefused,
    WorldModelRegistry,
    WorldModelVersion,
    model_use_gate,
    promote,
)

__all__ = [
    "CandidateObservation",
    "DatasetDefinition",
    "DatasetManifest",
    "Dimension",
    "ExperimentProposal",
    "FormulaCandidate",
    "FormulaCandidateRegistry",
    "InspectionFinding",
    "OutcomeKind",
    "ProcessCapabilityLedger",
    "ProcessOutcome",
    "PromotionRefused",
    "QualifiedStateDelta",
    "StateDeltaRefused",
    "Term",
    "ValidationEvidence",
    "WorldModelRegistry",
    "WorldModelVersion",
    "build_dataset",
    "model_use_gate",
    "promote",
    "propose_experiment",
    "qualify_state_delta",
    "route_failure",
    "update_with_evidence",
    "validate_safety_function",
]
