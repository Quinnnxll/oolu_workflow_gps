"""The vendor-neutral program IR — typed steps, bounded parameters.

The IR is the only thing the compiler accepts: an ordered sequence of
operations about workpieces, tools, fixtures, and approved profiles.
There is no free-text step, no motion stream, and the raw-command wall
from the router is enforced again here — a step parameter named
``gcode`` or ``joint_3_target`` is refused at construction, so servo
vocabulary cannot ride into the compiler inside an innocent-looking
parameter map (invariant 18, defense in depth).

The pilot's damper loop (§32) spells in these ops as: VERIFY_IDENTITY →
MOUNT_TOOL → ACQUIRE_WORKPIECE → PLACE_IN_FIXTURE → CLAMP →
EXECUTE_PROFILE → UNCLAMP → TRANSFER_TO_INSPECTION → PARK.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator

from ..actuation_routing.router import RAW_COMMAND_KEYS, RAW_COMMAND_PREFIXES


class IROp(str, Enum):
    VERIFY_IDENTITY = "verify_identity"
    MOUNT_TOOL = "mount_tool"
    ACQUIRE_WORKPIECE = "acquire_workpiece"
    PLACE_IN_FIXTURE = "place_in_fixture"
    CLAMP = "clamp"
    EXECUTE_PROFILE = "execute_profile"
    UNCLAMP = "unclamp"
    TRANSFER_TO_INSPECTION = "transfer_to_inspection"
    PARK = "park"


class RawParameterRejected(Exception):
    """Deliberately not a ValueError: pydantic would wrap that into a
    ValidationError, and this wall must be seen by its own name."""

    def __init__(self, key: str) -> None:
        super().__init__(f"raw machine-command parameter rejected in IR: {key!r}")
        self.key = key


class IRStep(BaseModel):
    """One typed operation with named, bounded parameters."""

    model_config = ConfigDict(frozen=True)

    op: IROp
    parameters: dict[str, str | int | bool]

    @field_validator("parameters")
    @classmethod
    def _no_raw_command_vocabulary(
        cls, parameters: dict[str, str | int | bool]
    ) -> dict[str, str | int | bool]:
        for key in parameters:
            lowered = key.lower()
            if lowered in RAW_COMMAND_KEYS or any(
                lowered.startswith(prefix) for prefix in RAW_COMMAND_PREFIXES
            ):
                raise RawParameterRejected(key)
        return parameters


class ProgramIR(BaseModel):
    model_config = ConfigDict(frozen=True)

    ir_id: str
    steps: tuple[IRStep, ...]
