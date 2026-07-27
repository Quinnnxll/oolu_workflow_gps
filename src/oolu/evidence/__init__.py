"""The evidence and observation spine — the protocol's feedback plane.

Stage R1 of docs/robotic-actuation-protocol-plan.md: the typed spine
through which physical reality becomes qualified evidence. Like its R0
sibling (``oolu.actuation``) this is a pure library — no transport, no
persistence beyond in-process reference stores (the durable layer is
R2) — and it pins the invariants that must hold before any service is
built on top:

* **raw evidence is immutable** (``manifest``, ``ingress``): chunks are
  content-hashed under a Merkle root, bundles are signed by registered
  devices, the store has no update or delete, and credential revocation
  blocks *new* uploads without touching history;
* **authentication is not metrological validity** (``grades``): the
  E0–E6 ladder is computed from separately established facts, and a
  perfect signature with an expired calibration still stops at E1;
* **grade at time of use** (``grades``): consumers record the grade and
  its basis when they rely on evidence; a later revocation flags those
  uses for review but never rewrites them;
* **corrections are new artifacts** (``observation``): calibration
  corrections and processing steps derive linked observations with
  reconstructable lineage back to raw chunks — never edits;
* **rights are named, separable permissions** (``rights``): permission
  for formula discovery never implies permission for model-weight
  training.
"""

from .calibration import (
    CalibrationCheck,
    CalibrationRecord,
    CalibrationRegistry,
    MeasurandCalibration,
    RevocationStatus,
)
from .grades import (
    EvidenceGrade,
    EvidenceLedger,
    GradeAtUse,
    GradeFacts,
    ReviewFlag,
    grade,
)
from .ingress import (
    DeviceRegistry,
    DeviceStatus,
    EvidenceIngress,
    HmacDeviceKeys,
    IngressRefusal,
)
from .manifest import RawChunk, RawEvidenceManifest, chunk_hash, merkle_root
from .observation import (
    LineageError,
    LineageLedger,
    Observation,
    ObservationResult,
    ProcessingStep,
    Uncertainty,
    apply_calibration_correction,
)
from .rights import EvidenceRights, RightsError, require_right

__all__ = [
    "CalibrationCheck",
    "CalibrationRecord",
    "CalibrationRegistry",
    "DeviceRegistry",
    "DeviceStatus",
    "EvidenceGrade",
    "EvidenceIngress",
    "EvidenceLedger",
    "EvidenceRights",
    "GradeAtUse",
    "GradeFacts",
    "HmacDeviceKeys",
    "IngressRefusal",
    "LineageError",
    "LineageLedger",
    "MeasurandCalibration",
    "Observation",
    "ObservationResult",
    "ProcessingStep",
    "RawChunk",
    "RawEvidenceManifest",
    "ReviewFlag",
    "RevocationStatus",
    "RightsError",
    "Uncertainty",
    "apply_calibration_correction",
    "chunk_hash",
    "grade",
    "merkle_root",
    "require_right",
]
