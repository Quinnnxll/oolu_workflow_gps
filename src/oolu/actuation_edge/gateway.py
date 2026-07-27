"""The local actuation gateway — verify the artifact, then the world.

The gateway sits between the compiled program and the machine, and it
adds two things to R0's arming gate:

* **Program-artifact verification** (acceptance test 21): before the
  digest/preflight/lease composition even runs, the artifact's text
  must hash to its digest, its compiler signature must verify, and its
  digest must be the one the job's aggregate digest bound. A modified
  program fails here, locally, by name.
* **Channel separation** (§24.7): the command identity that arms jobs
  and the evidence identity that signs execution bundles are distinct
  keys, validated distinct at construction. Evidence can never be
  signed with the command key, and a stolen evidence key arms nothing.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from ..actuation.contracts import ActuationJob
from ..actuation.lifecycle import ArmingDecision, arm
from ..actuation.preflight import PreflightResult
from ..evidence.ingress import HmacDeviceKeys
from .compiler import MachineProgram, verify_program
from .leases import DurableLeaseService


class ChannelIdentities(BaseModel):
    """Two identities, structurally never one."""

    model_config = ConfigDict(frozen=True)

    command_identity: str
    evidence_identity: str

    @model_validator(mode="after")
    def _distinct(self) -> "ChannelIdentities":
        if self.command_identity == self.evidence_identity:
            raise ValueError(
                "command and evidence channels must use distinct identities"
            )
        return self


class ActuationGateway:
    def __init__(
        self,
        *,
        identities: ChannelIdentities,
        keys: HmacDeviceKeys,
        leases: DurableLeaseService,
    ) -> None:
        self._identities = identities
        self._keys = keys
        self._leases = leases

    @property
    def evidence_identity(self) -> str:
        return self._identities.evidence_identity

    def verify_artifact(
        self, program: MachineProgram, job: ActuationJob
    ) -> tuple[str, ...]:
        failures = list(verify_program(program, keys=self._keys))
        if program.digest != job.machine_program_digest:
            failures.append("program_digest_not_bound_by_job")
        return tuple(failures)

    def arm(
        self,
        job: ActuationJob,
        *,
        approved_digest: str,
        program: MachineProgram,
        preflight: PreflightResult,
        lease_id: str,
        now: int,
    ) -> ArmingDecision:
        """Artifact verification, then R0's composed arming gate."""

        artifact_failures = self.verify_artifact(program, job)
        if artifact_failures:
            return ArmingDecision(armed=False, refusals=artifact_failures)
        return arm(
            job,
            approved_digest=approved_digest,
            preflight=preflight,
            leases=self._leases,
            lease_id=lease_id,
            now=now,
        )

    def sign_evidence(self, payload: bytes) -> str:
        """Evidence rides the evidence identity — never the command key."""

        return self._keys.sign(
            device_id=self._identities.evidence_identity, payload=payload
        )
