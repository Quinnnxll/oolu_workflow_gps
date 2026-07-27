"""Machine and sensor instances — status walls and capability queries.

Two rules from the plan are structural:

* **Selection is by capability, never by name** (acceptance test 17's
  substance at registry level): ``find_instruments`` answers "what can
  measure this quantity over this range at this uncertainty" and
  matches on measurand, calibrated range containment, and achievable
  uncertainty. The scoring router (R3) ranks what this returns; nothing
  here or there matches on a machine's name.
* **Status is a wall.** A quarantined or retired machine's sensors do
  not appear in capability queries, but their rows — and everything
  historically attributed to them — remain.

Calibration validity stays owned by R1's ``CalibrationRegistry``; the
query takes a predicate seam (``calibration_valid``) so there is one
source of calibration truth, not a copy in a second table.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from ..durable import DurableConnection

MACHINE_STATUSES = ("provisioning", "active", "maintenance", "quarantined", "retired")
SENSOR_STATUSES = ("active", "calibration_due", "drift_suspected", "failed", "retired")

_MACHINES_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_machines (
    machine_instance_id TEXT PRIMARY KEY,
    machine_definition TEXT NOT NULL,
    serial_number TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    status TEXT NOT NULL,
    registered_at INTEGER NOT NULL
)"""

_SENSORS_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_sensors (
    sensor_instance_id TEXT PRIMARY KEY,
    machine_instance_id TEXT NOT NULL,
    quantity_kind TEXT NOT NULL,
    unit TEXT NOT NULL,
    range_minimum REAL NOT NULL,
    range_maximum REAL NOT NULL,
    achievable_standard_uncertainty REAL NOT NULL,
    calibration_record_id TEXT,
    status TEXT NOT NULL,
    registered_at INTEGER NOT NULL
)"""


class MachineInstance(BaseModel):
    model_config = ConfigDict(frozen=True)

    machine_instance_id: str
    machine_definition: str
    serial_number: str
    facility_id: str
    status: str
    registered_at: int


class SensorInstance(BaseModel):
    model_config = ConfigDict(frozen=True)

    sensor_instance_id: str
    machine_instance_id: str
    quantity_kind: str
    unit: str
    range_minimum: float
    range_maximum: float
    achievable_standard_uncertainty: float
    calibration_record_id: str | None
    status: str
    registered_at: int


def _sensor_row(row) -> SensorInstance:
    return SensorInstance(
        sensor_instance_id=row["sensor_instance_id"],
        machine_instance_id=row["machine_instance_id"],
        quantity_kind=row["quantity_kind"],
        unit=row["unit"],
        range_minimum=row["range_minimum"],
        range_maximum=row["range_maximum"],
        achievable_standard_uncertainty=row["achievable_standard_uncertainty"],
        calibration_record_id=row["calibration_record_id"],
        status=row["status"],
        registered_at=row["registered_at"],
    )


class MachineRegistry:
    def __init__(self, conn: DurableConnection) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_MACHINES_SCHEMA)
            db.execute(_SENSORS_SCHEMA)

    def register_machine(
        self,
        *,
        machine_instance_id: str,
        machine_definition: str,
        serial_number: str,
        facility_id: str,
        now: int,
        status: str = "provisioning",
    ) -> MachineInstance:
        if status not in MACHINE_STATUSES:
            raise ValueError(f"unknown machine status {status!r}")
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO protocol_machines VALUES (?, ?, ?, ?, ?, ?)",
                (
                    machine_instance_id,
                    machine_definition,
                    serial_number,
                    facility_id,
                    status,
                    now,
                ),
            )
        return MachineInstance(
            machine_instance_id=machine_instance_id,
            machine_definition=machine_definition,
            serial_number=serial_number,
            facility_id=facility_id,
            status=status,
            registered_at=now,
        )

    def set_machine_status(self, machine_instance_id: str, status: str) -> None:
        if status not in MACHINE_STATUSES:
            raise ValueError(f"unknown machine status {status!r}")
        with self._conn.transaction() as db:
            current = db.execute(
                "SELECT status FROM protocol_machines WHERE machine_instance_id = ?",
                (machine_instance_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown machine {machine_instance_id!r}")
            if current["status"] == "retired":
                raise ValueError("a retired machine stays retired")
            db.execute(
                "UPDATE protocol_machines SET status = ? WHERE machine_instance_id = ?",
                (status, machine_instance_id),
            )

    def machine(self, machine_instance_id: str) -> MachineInstance | None:
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT * FROM protocol_machines WHERE machine_instance_id = ?",
                (machine_instance_id,),
            ).fetchone()
        if row is None:
            return None
        return MachineInstance(
            machine_instance_id=row["machine_instance_id"],
            machine_definition=row["machine_definition"],
            serial_number=row["serial_number"],
            facility_id=row["facility_id"],
            status=row["status"],
            registered_at=row["registered_at"],
        )

    def register_sensor(
        self,
        *,
        sensor_instance_id: str,
        machine_instance_id: str,
        quantity_kind: str,
        unit: str,
        range_minimum: float,
        range_maximum: float,
        achievable_standard_uncertainty: float,
        now: int,
        calibration_record_id: str | None = None,
        status: str = "active",
    ) -> SensorInstance:
        if status not in SENSOR_STATUSES:
            raise ValueError(f"unknown sensor status {status!r}")
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO protocol_sensors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sensor_instance_id,
                    machine_instance_id,
                    quantity_kind,
                    unit,
                    range_minimum,
                    range_maximum,
                    achievable_standard_uncertainty,
                    calibration_record_id,
                    status,
                    now,
                ),
            )
        return SensorInstance(
            sensor_instance_id=sensor_instance_id,
            machine_instance_id=machine_instance_id,
            quantity_kind=quantity_kind,
            unit=unit,
            range_minimum=range_minimum,
            range_maximum=range_maximum,
            achievable_standard_uncertainty=achievable_standard_uncertainty,
            calibration_record_id=calibration_record_id,
            status=status,
            registered_at=now,
        )

    def set_sensor_status(self, sensor_instance_id: str, status: str) -> None:
        if status not in SENSOR_STATUSES:
            raise ValueError(f"unknown sensor status {status!r}")
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE protocol_sensors SET status = ? WHERE sensor_instance_id = ?",
                (status, sensor_instance_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown sensor {sensor_instance_id!r}")

    def find_instruments(
        self,
        *,
        quantity_kind: str,
        unit: str,
        range_minimum: float,
        range_maximum: float,
        max_standard_uncertainty: float,
        calibration_valid: Callable[[SensorInstance], bool] | None = None,
    ) -> tuple[SensorInstance, ...]:
        """Every active instrument fit for the measurand — never by name.

        Fit means: same quantity kind and unit, the calibrated range
        contains the required range, achievable uncertainty is within
        the target, the sensor and its machine are active, and (when a
        predicate is wired to R1's calibration registry) the calibration
        holds.
        """

        with self._conn.transaction() as db:
            rows = db.execute(
                "SELECT s.* FROM protocol_sensors s"
                " JOIN protocol_machines m"
                " ON m.machine_instance_id = s.machine_instance_id"
                " WHERE s.quantity_kind = ? AND s.unit = ?"
                " AND s.range_minimum <= ? AND s.range_maximum >= ?"
                " AND s.achievable_standard_uncertainty <= ?"
                " AND s.status = 'active' AND m.status = 'active'"
                " ORDER BY s.achievable_standard_uncertainty",
                (
                    quantity_kind,
                    unit,
                    range_minimum,
                    range_maximum,
                    max_standard_uncertainty,
                ),
            ).fetchall()
        sensors = tuple(_sensor_row(row) for row in rows)
        if calibration_valid is None:
            return sensors
        return tuple(sensor for sensor in sensors if calibration_valid(sensor))
