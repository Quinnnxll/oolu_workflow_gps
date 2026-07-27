"""Workcells, tools, fixtures, and frames — registry truth for preflight.

This registry closes the loop the R0 preflight left open: where does
``LiveWorkcellState`` come from? Half of it is registry truth (which
machines and controllers a workcell version binds, its safety
configuration digest, the *current* coordinate-frame calibrations) and
half is edge observation (what tools are physically mounted, guard and
energy state). ``live_state`` composes both honestly — the registry
fills what it owns, the caller supplies what only the cell can see.

Three plan rules are structural here:

* **Frame calibrations are history, not state.** Recalibration inserts
  a new row; the live frame-graph digest is computed over each frame's
  current calibration, so a job authorized against the old digest fails
  preflight — and its aggregate digest cannot be quietly recomputed
  (acceptance test 27).
* **A worn or failed tool blocks the next job** (acceptance test 28):
  ``tool_release`` returns named failures for wear at limit, failed or
  overdue inspection, and status walls; ``release_check`` runs them for
  every tool a job binds, plus the workcell's commissioning status.
* **Only a qualified workcell releases work** — commissioning status is
  the registry half of review tier A5's "outside the certified cell".
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from ..actuation.contracts import ActuationJob
from ..actuation.digests import canonical_json, sha256_hex
from ..actuation.preflight import LiveWorkcellState
from ..durable import DurableConnection

FRAME_GRAPH_DOMAIN = b"oolu-frame-graph/v1\n"

COMMISSIONING_STATUSES = ("draft", "commissioning", "qualified", "suspended", "retired")
TOOL_STATUSES = ("available", "due_inspection", "quarantined", "retired")

_WORKCELLS_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_workcells (
    workcell_version_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL,
    machine_instance_ids TEXT NOT NULL,
    controller_instance_ids TEXT NOT NULL,
    safety_controller_instance_ids TEXT NOT NULL,
    safety_configuration_digest TEXT NOT NULL,
    risk_assessment_id TEXT NOT NULL,
    commissioning_status TEXT NOT NULL,
    registered_at INTEGER NOT NULL
)"""

_TOOLS_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_tools (
    tool_instance_id TEXT PRIMARY KEY,
    tool_type TEXT NOT NULL,
    serial_number TEXT NOT NULL,
    geometry_digest TEXT NOT NULL,
    wear_units INTEGER NOT NULL DEFAULT 0,
    wear_limit INTEGER NOT NULL,
    last_inspection_passed INTEGER,
    inspection_due_at INTEGER,
    status TEXT NOT NULL,
    registered_at INTEGER NOT NULL
)"""

_FIXTURES_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_fixtures (
    fixture_instance_id TEXT PRIMARY KEY,
    fixture_type TEXT NOT NULL,
    workcell_version_id TEXT NOT NULL,
    status TEXT NOT NULL,
    registered_at INTEGER NOT NULL
)"""

_FRAMES_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_frame_calibrations (
    workcell_version_id TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    calibration_digest TEXT NOT NULL,
    calibrated_at INTEGER NOT NULL,
    PRIMARY KEY (workcell_version_id, frame_id, calibrated_at)
)"""


class WorkcellRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    workcell_version_id: str
    facility_id: str
    machine_instance_ids: tuple[str, ...]
    controller_instance_ids: tuple[str, ...]
    safety_controller_instance_ids: tuple[str, ...]
    safety_configuration_digest: str
    risk_assessment_id: str
    commissioning_status: str
    registered_at: int


class WorkcellRegistry:
    def __init__(self, conn: DurableConnection) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_WORKCELLS_SCHEMA)
            db.execute(_TOOLS_SCHEMA)
            db.execute(_FIXTURES_SCHEMA)
            db.execute(_FRAMES_SCHEMA)

    # -- workcell versions -------------------------------------------------

    def register_workcell(
        self,
        *,
        workcell_version_id: str,
        facility_id: str,
        machine_instance_ids: tuple[str, ...],
        controller_instance_ids: tuple[str, ...],
        safety_controller_instance_ids: tuple[str, ...],
        safety_configuration_digest: str,
        risk_assessment_id: str,
        now: int,
    ) -> WorkcellRow:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO protocol_workcells"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)",
                (
                    workcell_version_id,
                    facility_id,
                    json.dumps(sorted(machine_instance_ids)),
                    json.dumps(sorted(controller_instance_ids)),
                    json.dumps(sorted(safety_controller_instance_ids)),
                    safety_configuration_digest,
                    risk_assessment_id,
                    now,
                ),
            )
        return self.workcell(workcell_version_id)

    def set_commissioning_status(self, workcell_version_id: str, status: str) -> None:
        if status not in COMMISSIONING_STATUSES:
            raise ValueError(f"unknown commissioning status {status!r}")
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT commissioning_status FROM protocol_workcells"
                " WHERE workcell_version_id = ?",
                (workcell_version_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown workcell {workcell_version_id!r}")
            if row["commissioning_status"] == "retired":
                raise ValueError("a retired workcell version stays retired")
            db.execute(
                "UPDATE protocol_workcells SET commissioning_status = ?"
                " WHERE workcell_version_id = ?",
                (status, workcell_version_id),
            )

    def workcell(self, workcell_version_id: str) -> WorkcellRow | None:
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT * FROM protocol_workcells WHERE workcell_version_id = ?",
                (workcell_version_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkcellRow(
            workcell_version_id=row["workcell_version_id"],
            facility_id=row["facility_id"],
            machine_instance_ids=tuple(json.loads(row["machine_instance_ids"])),
            controller_instance_ids=tuple(json.loads(row["controller_instance_ids"])),
            safety_controller_instance_ids=tuple(
                json.loads(row["safety_controller_instance_ids"])
            ),
            safety_configuration_digest=row["safety_configuration_digest"],
            risk_assessment_id=row["risk_assessment_id"],
            commissioning_status=row["commissioning_status"],
            registered_at=row["registered_at"],
        )

    # -- tools -------------------------------------------------------------

    def register_tool(
        self,
        *,
        tool_instance_id: str,
        tool_type: str,
        serial_number: str,
        geometry_digest: str,
        wear_limit: int,
        now: int,
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO protocol_tools"
                " (tool_instance_id, tool_type, serial_number, geometry_digest,"
                "  wear_units, wear_limit, last_inspection_passed,"
                "  inspection_due_at, status, registered_at)"
                " VALUES (?, ?, ?, ?, 0, ?, NULL, NULL, 'available', ?)",
                (
                    tool_instance_id,
                    tool_type,
                    serial_number,
                    geometry_digest,
                    wear_limit,
                    now,
                ),
            )

    def record_tool_wear(self, tool_instance_id: str, units: int) -> None:
        if units < 0:
            raise ValueError("wear only accumulates")
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE protocol_tools SET wear_units = wear_units + ?"
                " WHERE tool_instance_id = ?",
                (units, tool_instance_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown tool {tool_instance_id!r}")

    def record_tool_inspection(
        self,
        tool_instance_id: str,
        *,
        passed: bool,
        now: int,
        next_due_at: int | None = None,
    ) -> None:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE protocol_tools SET last_inspection_passed = ?,"
                " inspection_due_at = ?, status = ?"
                " WHERE tool_instance_id = ?",
                (
                    1 if passed else 0,
                    next_due_at,
                    "available" if passed else "quarantined",
                    tool_instance_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown tool {tool_instance_id!r}")

    def set_tool_status(self, tool_instance_id: str, status: str) -> None:
        if status not in TOOL_STATUSES:
            raise ValueError(f"unknown tool status {status!r}")
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE protocol_tools SET status = ? WHERE tool_instance_id = ?",
                (status, tool_instance_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown tool {tool_instance_id!r}")

    def tool_release(self, tool_instance_id: str, *, now: int) -> tuple[str, ...]:
        """Named reasons this tool cannot serve the next job."""

        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT * FROM protocol_tools WHERE tool_instance_id = ?",
                (tool_instance_id,),
            ).fetchone()
        if row is None:
            return (f"unknown_tool:{tool_instance_id}",)
        failures: list[str] = []
        if row["status"] != "available":
            failures.append(f"tool_{row['status']}:{tool_instance_id}")
        if row["wear_units"] >= row["wear_limit"]:
            failures.append(f"tool_wear_limit_reached:{tool_instance_id}")
        if row["last_inspection_passed"] == 0:
            failures.append(f"tool_inspection_failed:{tool_instance_id}")
        if row["inspection_due_at"] is not None and now > row["inspection_due_at"]:
            failures.append(f"tool_inspection_overdue:{tool_instance_id}")
        return tuple(failures)

    # -- fixtures ----------------------------------------------------------

    def register_fixture(
        self,
        *,
        fixture_instance_id: str,
        fixture_type: str,
        workcell_version_id: str,
        now: int,
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO protocol_fixtures VALUES (?, ?, ?, 'available', ?)",
                (fixture_instance_id, fixture_type, workcell_version_id, now),
            )

    # -- coordinate frames -------------------------------------------------

    def calibrate_frame(
        self,
        *,
        workcell_version_id: str,
        frame_id: str,
        calibration_digest: str,
        now: int,
    ) -> None:
        """Insert a new calibration; prior rows remain as history."""

        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO protocol_frame_calibrations VALUES (?, ?, ?, ?)",
                (workcell_version_id, frame_id, calibration_digest, now),
            )

    def frame_graph_digest(self, workcell_version_id: str) -> str:
        """The digest over every frame's *current* calibration.

        Any recalibration changes this value, which is exactly what
        invalidates programs authorized against the previous digest.
        """

        with self._conn.transaction() as db:
            rows = db.execute(
                "SELECT frame_id, calibration_digest FROM"
                " protocol_frame_calibrations c"
                " WHERE workcell_version_id = ? AND calibrated_at = ("
                "   SELECT MAX(calibrated_at) FROM protocol_frame_calibrations"
                "   WHERE workcell_version_id = c.workcell_version_id"
                "   AND frame_id = c.frame_id)",
                (workcell_version_id,),
            ).fetchall()
        if not rows:
            raise KeyError(f"workcell {workcell_version_id!r} has no calibrated frames")
        graph = {row["frame_id"]: row["calibration_digest"] for row in rows}
        return sha256_hex(FRAME_GRAPH_DOMAIN + canonical_json(graph))

    # -- composition into preflight ---------------------------------------

    def release_check(self, job: ActuationJob, *, now: int) -> tuple[str, ...]:
        """Registry-owned reasons a job cannot proceed to preflight."""

        failures: list[str] = []
        cell = self.workcell(job.workcell_version_id)
        if cell is None:
            failures.append(f"unknown_workcell:{job.workcell_version_id}")
        elif cell.commissioning_status != "qualified":
            failures.append(f"workcell_not_qualified:{cell.commissioning_status}")
        for tool_id in job.tool_instance_ids:
            failures.extend(self.tool_release(tool_id, now=now))
        return tuple(failures)

    def live_state(
        self,
        workcell_version_id: str,
        *,
        tool_instance_ids: tuple[str, ...],
        fixture_instance_ids: tuple[str, ...],
        workpiece_instance_ids: tuple[str, ...],
        machine_program_digest: str,
        guards_closed: bool,
        emergency_stop_clear: bool,
        safety_controller_healthy: bool,
        energy_state_nominal: bool,
        clock_within_tolerance: bool,
    ) -> LiveWorkcellState:
        """Registry truth plus edge observation, composed for preflight.

        The registry fills what it owns — bound machines, controllers,
        safety configuration, the current frame-graph digest. The caller
        supplies only what the edge physically observes.
        """

        cell = self.workcell(workcell_version_id)
        if cell is None:
            raise KeyError(f"unknown workcell {workcell_version_id!r}")
        return LiveWorkcellState(
            workcell_version_id=cell.workcell_version_id,
            machine_instance_ids=cell.machine_instance_ids,
            controller_instance_ids=cell.controller_instance_ids,
            tool_instance_ids=tool_instance_ids,
            fixture_instance_ids=fixture_instance_ids,
            workpiece_instance_ids=workpiece_instance_ids,
            coordinate_frame_graph_digest=self.frame_graph_digest(workcell_version_id),
            machine_program_digest=machine_program_digest,
            local_safety_configuration_digest=cell.safety_configuration_digest,
            guards_closed=guards_closed,
            emergency_stop_clear=emergency_stop_clear,
            safety_controller_healthy=safety_controller_healthy,
            energy_state_nominal=energy_state_nominal,
            clock_within_tolerance=clock_within_tolerance,
        )
