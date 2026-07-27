"""Authorized test jobs — the §9 digest, and quarantine at birth.

The test-job digest binds exactly the fields §9 names: specimens,
method version, machine, sensors, controller configuration, target
conditions, data rights, approval policy, and expiration. Any material
change is a new digest and a new authorization cycle.

A job citing a method version the method registry has not approved is
not refused into nonexistence — it is stored **quarantined** with a
named reason (acceptance test 3), so the attempt itself remains
evidence.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from ..actuation.digests import canonical_json, sha256_hex
from ..durable import DurableConnection
from .methods import MethodRegistry

TEST_JOB_DOMAIN = b"oolu-test-job/v1\n"

_SCHEMA = """CREATE TABLE IF NOT EXISTS protocol_test_jobs (
    test_job_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    authorization_digest TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    authorized_at INTEGER NOT NULL
)"""


class AuthorizedTestJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    test_job_id: str
    specimen_instance_ids: tuple[str, ...]
    method_id: str
    method_version: str
    machine_instance_id: str
    sensor_instance_ids: tuple[str, ...]
    controller_configuration_digest: str
    target_conditions: dict[str, str | int | bool]
    data_rights_policy_id: str
    approval_policy_id: str
    expires_at: int
    status: str  # authorized | quarantined
    reason: str | None = None
    authorization_digest: str | None = None


def authorization_digest(job: AuthorizedTestJob) -> str:
    """The §9 authorization digest over the bound fields, canonically."""

    return sha256_hex(
        TEST_JOB_DOMAIN
        + canonical_json(
            {
                "specimen_instance_ids": sorted(job.specimen_instance_ids),
                "method_id": job.method_id,
                "method_version": job.method_version,
                "machine_instance_id": job.machine_instance_id,
                "sensor_instance_ids": sorted(job.sensor_instance_ids),
                "controller_configuration_digest": (
                    job.controller_configuration_digest
                ),
                "target_conditions": dict(job.target_conditions),
                "data_rights_policy_id": job.data_rights_policy_id,
                "approval_policy_id": job.approval_policy_id,
                "expires_at": job.expires_at,
            }
        )
    )


class AuthorizedTestJobRegistry:
    def __init__(self, conn: DurableConnection, methods: MethodRegistry) -> None:
        self._conn = conn
        self._methods = methods
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def authorize(
        self,
        *,
        test_job_id: str,
        specimen_instance_ids: tuple[str, ...],
        method_id: str,
        method_version: str,
        machine_instance_id: str,
        sensor_instance_ids: tuple[str, ...],
        controller_configuration_digest: str,
        target_conditions: dict[str, str | int | bool],
        data_rights_policy_id: str,
        approval_policy_id: str,
        expires_at: int,
        now: int,
    ) -> AuthorizedTestJob:
        job = AuthorizedTestJob(
            test_job_id=test_job_id,
            specimen_instance_ids=specimen_instance_ids,
            method_id=method_id,
            method_version=method_version,
            machine_instance_id=machine_instance_id,
            sensor_instance_ids=sensor_instance_ids,
            controller_configuration_digest=controller_configuration_digest,
            target_conditions=target_conditions,
            data_rights_policy_id=data_rights_policy_id,
            approval_policy_id=approval_policy_id,
            expires_at=expires_at,
            status="quarantined",
        )
        if not self._methods.authorized(method_id, method_version):
            job = job.model_copy(
                update={
                    "reason": (
                        f"unauthorized_method_version:{method_id}@{method_version}"
                    )
                }
            )
        else:
            job = job.model_copy(
                update={
                    "status": "authorized",
                    "authorization_digest": authorization_digest(job),
                }
            )
        with self._conn.transaction() as db:
            db.execute(
                "INSERT INTO protocol_test_jobs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job.test_job_id,
                    job.model_dump_json(),
                    job.authorization_digest,
                    job.status,
                    job.reason,
                    now,
                ),
            )
        return job

    def get(self, test_job_id: str) -> AuthorizedTestJob | None:
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT payload_json FROM protocol_test_jobs WHERE test_job_id = ?",
                (test_job_id,),
            ).fetchone()
        if row is None:
            return None
        return AuthorizedTestJob(**json.loads(row["payload_json"]))

    def verify(self, job: AuthorizedTestJob) -> bool:
        """Recompute the digest; a changed binding cannot verify."""

        if job.status != "authorized" or job.authorization_digest is None:
            return False
        return authorization_digest(job) == job.authorization_digest
