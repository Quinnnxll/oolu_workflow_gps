"""Calibration records with validity windows and revocation that never
deletes.

The registry owns exactly one fact for the grade ladder: whether a
calibration record was valid *for a given measurand, at a given value,
at a given time*. Checks return named failures (the R0 idiom) so an
expired record and a revoked record stay distinguishable all the way
into the evidence review trail — and revocation is a status transition
with a timestamp, never a removal, so historical checks against
pre-revocation test times still answer honestly.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class RevocationStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class MeasurandCalibration(BaseModel):
    """One calibrated quantity: kind, unit, and the covered range."""

    model_config = ConfigDict(frozen=True)

    quantity_kind: str
    unit: str
    range_minimum: float
    range_maximum: float
    correction_offset: float = 0.0
    correction_gain: float = 1.0
    standard_uncertainty: float | None = None

    def covers(self, value: float) -> bool:
        return self.range_minimum <= value <= self.range_maximum


class CalibrationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    calibration_record_id: str
    sensor_or_instrument_id: str
    calibration_provider_id: str
    method_id: str
    calibration_date: int
    valid_until: int | None
    measurands: tuple[MeasurandCalibration, ...]
    reference_standard_chain: tuple[str, ...] = ()
    signature: str = ""

    def measurand(self, quantity_kind: str, unit: str) -> MeasurandCalibration | None:
        for entry in self.measurands:
            if entry.quantity_kind == quantity_kind and entry.unit == unit:
                return entry
        return None


class CalibrationCheck(BaseModel):
    """The §7.3 preflight verdict for one record against one reading."""

    model_config = ConfigDict(frozen=True)

    failures: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.failures


class CalibrationRegistry:
    """Records and their revocation history. Nothing is ever deleted."""

    def __init__(self) -> None:
        self._records: dict[str, CalibrationRecord] = {}
        self._revocations: dict[str, tuple[RevocationStatus, int]] = {}

    def register(self, record: CalibrationRecord) -> None:
        if record.calibration_record_id in self._records:
            raise ValueError(
                f"calibration record {record.calibration_record_id!r} "
                "already registered; records are immutable"
            )
        self._records[record.calibration_record_id] = record

    def get(self, record_id: str) -> CalibrationRecord | None:
        return self._records.get(record_id)

    def revoke(
        self,
        record_id: str,
        *,
        at: int,
        status: RevocationStatus = RevocationStatus.REVOKED,
    ) -> None:
        if record_id not in self._records:
            raise KeyError(f"unknown calibration record {record_id!r}")
        self._revocations[record_id] = (status, at)

    def status_at(self, record_id: str, *, at: int) -> RevocationStatus:
        revocation = self._revocations.get(record_id)
        if revocation is None:
            return RevocationStatus.ACTIVE
        status, effective_at = revocation
        return status if at >= effective_at else RevocationStatus.ACTIVE

    def check(
        self,
        record_id: str,
        *,
        quantity_kind: str,
        unit: str,
        value: float,
        test_time: int,
        checked_at: int | None = None,
    ) -> CalibrationCheck:
        """Named failures for one reading against one record.

        ``test_time`` is when the measurement was taken — expiry is
        judged there. Revocation is judged at ``checked_at`` (defaults
        to ``test_time``): a record revoked *after* a test still fails
        a later check, which is exactly what triggers the review flags
        on grade-at-time-of-use records.
        """

        failures: list[str] = []
        record = self._records.get(record_id)
        if record is None:
            return CalibrationCheck(failures=("unknown_calibration_record",))
        if test_time < record.calibration_date:
            failures.append("calibration_not_yet_effective")
        if record.valid_until is not None and test_time > record.valid_until:
            failures.append("calibration_expired")
        status = self.status_at(
            record_id, at=test_time if checked_at is None else checked_at
        )
        if status is not RevocationStatus.ACTIVE:
            failures.append(f"calibration_{status.value}")
        entry = record.measurand(quantity_kind, unit)
        if entry is None:
            failures.append("measurand_mismatch")
        elif not entry.covers(value):
            failures.append("value_outside_calibrated_range")
        return CalibrationCheck(failures=tuple(failures))
