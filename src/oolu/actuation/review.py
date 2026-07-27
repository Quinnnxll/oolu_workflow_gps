"""Human review tiers — §24.6's threshold engine, consequence-keyed.

The tier is computed from declared signals about the job, and the
computation is deliberately monotone in danger: prohibition beats dual
control beats engineer review beats operator review beats automatic
repeat. Two separations the plan insists on are structural here:

* **Safety approval is not budget approval.** The decision carries the
  required *safety* roles; financial authorization is a different
  decision made elsewhere (OoLu's existing approval spine).
* **Outside the certified cell or validated envelope, there is no
  tier to argue about** — physical execution is prohibited (A5), full
  stop.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class ReviewTier(str, Enum):
    A0_SIMULATION_ONLY = "A0"
    A1_SUPERVISED_COMMISSIONING = "A1"
    A2_VALIDATED_REPEAT = "A2"
    A3_MATERIAL_CHANGE = "A3"
    A4_HIGH_CONSEQUENCE = "A4"
    A5_PROHIBITED = "A5"


class ReviewSignals(BaseModel):
    """Declared facts about the proposed job that drive the tier."""

    model_config = ConfigDict(frozen=True)

    physical_execution_requested: bool
    within_certified_cell: bool
    within_validated_envelope: bool
    previously_validated_repeat: bool = False
    new_material_or_supplier_lot: bool = False
    changed_tool_fixture_frame_or_recipe: bool = False
    first_article: bool = False
    irreversible_or_high_energy: bool = False
    hazardous_material: bool = False
    human_robot_shared_space: bool = False
    safety_critical_product: bool = False


class ReviewDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    tier: ReviewTier
    physical_execution_permitted: bool
    required_safety_roles: tuple[str, ...]
    continuous_supervision: bool = False
    dual_control: bool = False


def assign_tier(signals: ReviewSignals) -> ReviewDecision:
    if not signals.physical_execution_requested:
        return ReviewDecision(
            tier=ReviewTier.A0_SIMULATION_ONLY,
            physical_execution_permitted=False,
            required_safety_roles=(),
        )
    if not (signals.within_certified_cell and signals.within_validated_envelope):
        return ReviewDecision(
            tier=ReviewTier.A5_PROHIBITED,
            physical_execution_permitted=False,
            required_safety_roles=(),
        )
    if (
        signals.irreversible_or_high_energy
        or signals.hazardous_material
        or signals.human_robot_shared_space
        or signals.safety_critical_product
    ):
        return ReviewDecision(
            tier=ReviewTier.A4_HIGH_CONSEQUENCE,
            physical_execution_permitted=True,
            required_safety_roles=("process_owner", "safety_or_quality_owner"),
            dual_control=True,
        )
    if (
        signals.new_material_or_supplier_lot
        or signals.changed_tool_fixture_frame_or_recipe
        or signals.first_article
    ):
        return ReviewDecision(
            tier=ReviewTier.A3_MATERIAL_CHANGE,
            physical_execution_permitted=True,
            required_safety_roles=("process_engineer",),
        )
    if signals.previously_validated_repeat:
        return ReviewDecision(
            tier=ReviewTier.A2_VALIDATED_REPEAT,
            physical_execution_permitted=True,
            required_safety_roles=(),
        )
    return ReviewDecision(
        tier=ReviewTier.A1_SUPERVISED_COMMISSIONING,
        physical_execution_permitted=True,
        required_safety_roles=("qualified_operator",),
        continuous_supervision=True,
    )
