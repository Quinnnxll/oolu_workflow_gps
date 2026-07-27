"""Capability nodes — physical equipment on the capability web.

One contract for everything with a motor or a measurand: a six-axis
robot, a CNC mill, a dynamometer, and an environmental chamber are all
an ``ActuationNode`` wrapping an R0 ``ActuationCapability`` — they
differ through declared effects, constraints, hazards, and bindings,
never through their own routing logic (Phase 6's second exit gate).

Nodes carry what routing needs and nothing an edge would execute:
bindings into the R2 registries, declared producible properties,
margins, availability, cost, and history. Scores are computed *from*
these fields by the router; a node cannot bring its own scoring.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..actuation.contracts import ActuationCapability


class MeasurementNode(BaseModel):
    """An instrument, laboratory, or inspection route on the web."""

    model_config = ConfigDict(frozen=True)

    node_version_id: str
    observed_property: str
    unit: str
    range_minimum: float
    range_maximum: float
    achievable_standard_uncertainty: float
    supported_method_ids: tuple[str, ...]
    machine_instance_id: str
    sensor_instance_ids: tuple[str, ...]
    facility_id: str
    evidence_grade_ceiling: str
    availability: float = 1.0  # 0..1 share of the window it can serve
    cost: float = 0.0
    delay: float = 0.0


class ActuationNode(BaseModel):
    """A robot, cell, or specialized machine offering a physical effect."""

    model_config = ConfigDict(frozen=True)

    node_version_id: str
    capability: ActuationCapability
    workcell_version_id: str
    machine_instance_ids: tuple[str, ...]
    facility_id: str
    # What the node can make true about a workpiece — the routable face
    # of the capability's produced-state-delta schema.
    producible_properties: tuple[str, ...]
    accepted_input_properties: dict[str, str | int | bool] = Field(default_factory=dict)
    available_tool_types: tuple[str, ...] = ()
    # Declared recovery routes; mandatory before an irreversible
    # capability may even be proposed (Phase 6's fourth exit gate).
    disposition_paths: tuple[str, ...] = ()
    safety_case_current: bool = False
    achievable_tolerance_um: int | None = None
    workspace_and_payload_margin: float = 0.0
    verification_coverage: float = 0.0
    availability: float = 1.0
    historical_yield: float = 0.0
    process_drift_risk: float = 0.0
    cost: float = 0.0
    delay: float = 0.0
