"""Local execution semantics — what the cell does when the cloud can't.

Everything here encodes a behavioral contract the plan makes
non-negotiable, with the physical world injected as seams (a plant
callable supplies actuals; the safety controller is its own object):

* **Cloud loss changes nothing locally** (acceptance test 24): no step,
  deviation, or safety path consults the cloud link. Losing
  connectivity mid-cycle is recorded and the cycle continues under the
  locally validated program; the safety controller trips exactly as
  well with everything dark.
* **Lease expiry is not a stop** (acceptance test 25): expiry during a
  cycle blocks the *next* start and hands the current cycle to the
  controller's validated completion behavior. There is no cloud stop
  path to invoke.
* **Deviation beyond bound → local hold, immutable evidence**
  (acceptance test 26): commanded-versus-actual is checked per step
  against approved bounds; exceeding one causes a local safe stop and
  a deviation event that rides the signed evidence bundle.
* **No automatic restart after a trip** (acceptance test 31): a safety
  or deviation stop refuses ``restart`` by name; the way back is a new
  proposal through every gate.
* **A first piece is not a batch** (acceptance test 29): the batch gate
  refuses until the first piece's independent inspection passes.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from pydantic import BaseModel, ConfigDict

from ..actuation.contracts import ActuationJob, Disposition, ExecutionEvidenceBundle
from ..actuation.digests import canonical_json
from ..actuation.lifecycle import ActuationState, transition
from .compiler import MachineProgram
from .gateway import ActuationGateway
from .program_ir import IRStep, ProgramIR


class ExecutionMode(str, Enum):
    DRY_RUN = "dry_run"
    REDUCED_ENERGY = "reduced_energy"
    FIRST_PIECE = "first_piece"
    VALIDATED_REPEAT = "validated_repeat"


class SafetyControllerSim:
    """The independent local safety function: no cloud, no gateway, no
    model input — it trips because its own sensors say so, always."""

    def __init__(self) -> None:
        self.tripped = False
        self.trip_reason: str | None = None

    def trip(self, reason: str) -> None:
        self.tripped = True
        self.trip_reason = reason


class CloudLink:
    connected: bool

    def __init__(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False


class DeviationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_index: int
    parameter: str
    commanded: int
    actual: int
    limit: int


class DeviationMonitor:
    def __init__(self, limits: dict[str, int]) -> None:
        self._limits = dict(limits)

    def check(
        self, step_index: int, commanded: dict[str, int], actual: dict[str, int]
    ) -> tuple[DeviationEvent, ...]:
        events: list[DeviationEvent] = []
        for parameter, commanded_value in commanded.items():
            limit = self._limits.get(parameter)
            if limit is None:
                continue
            actual_value = actual.get(parameter, commanded_value)
            if abs(actual_value - commanded_value) > limit:
                events.append(
                    DeviationEvent(
                        step_index=step_index,
                        parameter=parameter,
                        commanded=commanded_value,
                        actual=actual_value,
                        limit=limit,
                    )
                )
        return tuple(events)


class ExecutionRefused(RuntimeError):
    pass


class LocalExecutor:
    """One armed cycle, step by step, under local rules only."""

    def __init__(
        self,
        *,
        job: ActuationJob,
        program: MachineProgram,
        ir: ProgramIR,
        mode: ExecutionMode,
        monitor: DeviationMonitor,
        safety: SafetyControllerSim,
        cloud: CloudLink,
        gateway: ActuationGateway,
    ) -> None:
        self._job = job
        self._program = program
        self._ir = ir
        self.mode = mode
        self._monitor = monitor
        self._safety = safety
        self._cloud = cloud
        self._gateway = gateway
        self.state = ActuationState.ARMED
        self.deviation_events: tuple[DeviationEvent, ...] = ()
        self.stop_reason: str | None = None
        self.new_start_blocked = False
        self.cloud_loss_noted = False

    def execute(
        self, plant: Callable[[IRStep], dict[str, int]], *, started_at: str
    ) -> None:
        """Run every step against the plant, or stop locally by rule.

        The cloud link is deliberately consulted nowhere in this loop:
        connectivity influences neither stepping, nor deviation, nor
        safety. A disconnect mid-cycle is noted for the record and the
        locally validated cycle continues (acceptance test 24).
        """

        if self.state is not ActuationState.ARMED:
            raise ExecutionRefused(f"cannot execute from {self.state.value}")
        self._started_at = started_at
        self.state = transition(self.state, ActuationState.EXECUTING)
        for index, step in enumerate(self._ir.steps, start=1):
            if not self._cloud.connected:
                self.cloud_loss_noted = True  # recorded, never acted on
            if self._safety.tripped:
                self._safe_stop(f"safety_function_demanded:{self._safety.trip_reason}")
                return
            commanded = {
                key: value
                for key, value in step.parameters.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
            actual = plant(step)
            events = self._monitor.check(index, commanded, actual)
            if events:
                self.deviation_events = (*self.deviation_events, *events)
                self._safe_stop("commanded_actual_deviation_beyond_bound")
                return
        self.state = transition(self.state, ActuationState.VERIFYING)

    def _safe_stop(self, reason: str) -> None:
        self.state = transition(self.state, ActuationState.SAFE_STOPPED)
        self.stop_reason = reason
        self.new_start_blocked = True

    def on_lease_expired(self) -> str:
        """Expiry mid-cycle: block the next start, never stop this one."""

        self.new_start_blocked = True
        if self.state is ActuationState.EXECUTING:
            return "controller_validated_completion"
        return "new_start_blocked"

    def restart(self) -> None:
        if self.state is ActuationState.SAFE_STOPPED:
            raise ExecutionRefused("automatic_restart_prohibited_after_safety_stop")
        raise ExecutionRefused("restart_requires_a_new_proposal_through_every_gate")

    def finish(
        self, *, disposition: Disposition, ended_at: str
    ) -> ExecutionEvidenceBundle:
        """Assemble and sign the evidence bundle on the evidence channel."""

        unsigned = ExecutionEvidenceBundle(
            job_digest=self._job.aggregate(),
            program_digest=self._program.digest,
            started_at=self._started_at,
            ended_at=ended_at,
            commanded_trace_ref=f"trace://commanded/{self._job.job_id}",
            actual_trace_ref=f"trace://actual/{self._job.job_id}",
            deviation_events=tuple(
                {
                    "step_index": event.step_index,
                    "parameter": event.parameter,
                    "commanded": event.commanded,
                    "actual": event.actual,
                    "limit": event.limit,
                }
                for event in self.deviation_events
            ),
            interlock_and_alarm_events=(
                ({"event": self.stop_reason},) if self.stop_reason else ()
            ),
            pre_workpiece_state_id=self._job.input_state_digest,
            post_workpiece_state_id="",
            disposition=disposition,
            edge_signature="",
        )
        payload = canonical_json(unsigned.model_dump(mode="json"))
        return unsigned.model_copy(
            update={"edge_signature": self._gateway.sign_evidence(payload)}
        )


class BatchGate:
    """First-piece discipline: no batch without inspection approval."""

    def __init__(self) -> None:
        self._first_pieces: dict[str, Disposition] = {}
        self._inspections: dict[str, bool] = {}

    def record_first_piece(self, job_digest: str, *, disposition: Disposition) -> None:
        self._first_pieces[job_digest] = disposition

    def record_inspection(self, job_digest: str, *, passed: bool) -> None:
        if job_digest not in self._first_pieces:
            raise KeyError("no first piece recorded for this job digest")
        self._inspections[job_digest] = passed

    def authorize_batch(self, job_digest: str) -> tuple[str, ...]:
        """Named reasons batch execution cannot begin. Empty means go."""

        if job_digest not in self._first_pieces:
            return ("first_piece_required",)
        refusals: list[str] = []
        if self._first_pieces[job_digest] is not Disposition.ACCEPTED:
            refusals.append(f"first_piece_{self._first_pieces[job_digest].value}")
        if job_digest not in self._inspections:
            refusals.append("first_piece_inspection_pending")
        elif not self._inspections[job_digest]:
            refusals.append("first_piece_inspection_failed")
        return tuple(refusals)
