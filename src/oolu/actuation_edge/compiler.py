"""The deterministic compiler — eligible release in, signed bytes out.

The compiler is the single point where a typed plan becomes something a
controller can hold, and it is treated as the single point of trust the
plan review named (gap 2.2.4):

* **No door but the typed one.** The inputs are an R3 release verdict,
  the plan it released, and a validated IR. There is no text input, no
  raw-program passthrough, and an ineligible or mismatched release
  refuses before anything renders — approvals cannot be bypassed by
  calling the compiler directly.
* **Byte determinism.** Rendering sorts every parameter, forbids
  wall-clock or randomness, and emits exactly one text form; the same
  plan and dialect produce the same bytes and the same digest, forever.
  The golden-program regression suite in ``tests/goldens/`` holds the
  reference bytes — a rendering change that alters any golden is a
  reviewable event, never an accident.
* **Signed artifacts.** The program digest is domain-separated over the
  rendered bytes, and the artifact is signed by the compiler's own
  identity — the gateway later verifies both digest and signature
  before arming (acceptance test 21).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..actuation.digests import sha256_hex
from ..actuation_routing.planner import ActuationPlan, ReleaseVerdict
from ..evidence.ingress import HmacDeviceKeys
from .program_ir import ProgramIR

PROGRAM_DOMAIN = b"oolu-machine-program/v1\n"


class CompileRefused(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class MachineProgram(BaseModel):
    """The immutable compiled artifact a controller receives."""

    model_config = ConfigDict(frozen=True)

    program_id: str
    dialect: str
    plan_digest: str
    text: str
    digest: str
    compiler_identity: str
    signature: str


class PilotDialect:
    """The reference controller dialect: line-oriented, sorted, versioned.

    A real vendor adapter (robot controller, CNC post-processor, PLC
    recipe writer) implements this same ``render`` contract; the pilot
    dialect exists so determinism and the golden suite are testable
    without hardware.
    """

    name = "oolu-pilot/1"

    def render(self, plan: ActuationPlan, ir: ProgramIR) -> str:
        lines = [self.name, f"PLAN {plan.digest()}"]
        for name in sorted(plan.process_parameters):
            lines.append(f"PARAM {name}={plan.process_parameters[name]}")
        for index, step in enumerate(ir.steps, start=1):
            rendered = " ".join(
                f"{key}={step.parameters[key]}" for key in sorted(step.parameters)
            )
            lines.append(f"STEP {index} {step.op.value.upper()} {rendered}".rstrip())
        lines.append("END")
        return "\n".join(lines) + "\n"


def program_digest(text: str) -> str:
    return sha256_hex(PROGRAM_DOMAIN + text.encode("utf-8"))


def compile_program(
    release: ReleaseVerdict,
    plan: ActuationPlan,
    ir: ProgramIR,
    *,
    program_id: str,
    dialect: PilotDialect,
    signer: HmacDeviceKeys,
    compiler_identity: str,
) -> MachineProgram:
    """Deterministically compile an eligible release, or refuse by name."""

    reasons: list[str] = []
    if not release.eligible_for_compilation:
        reasons.append("release_not_eligible")
    if release.plan_digest != plan.digest():
        reasons.append("release_for_different_plan")
    if not ir.steps:
        reasons.append("empty_program")
    if reasons:
        raise CompileRefused(tuple(reasons))
    text = dialect.render(plan, ir)
    digest = program_digest(text)
    signature = signer.sign(
        device_id=compiler_identity, payload=PROGRAM_DOMAIN + digest.encode("ascii")
    )
    return MachineProgram(
        program_id=program_id,
        dialect=dialect.name,
        plan_digest=release.plan_digest,
        text=text,
        digest=digest,
        compiler_identity=compiler_identity,
        signature=signature,
    )


def verify_program(program: MachineProgram, *, keys: HmacDeviceKeys) -> tuple[str, ...]:
    """Named reasons the artifact cannot be trusted as compiled."""

    failures: list[str] = []
    if program_digest(program.text) != program.digest:
        failures.append("program_text_digest_mismatch")
    if not keys.verify(
        device_id=program.compiler_identity,
        payload=PROGRAM_DOMAIN + program.digest.encode("ascii"),
        signature=program.signature,
    ):
        failures.append("program_signature_invalid")
    return tuple(failures)
