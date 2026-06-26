"""Runtime contract — the HOST (consumer) side of the script<->runtime protocol.

The sandbox runs a synthesized script inside a container and captures its raw
stdout. That stdout is a mix of diagnostics (logging, warnings, debug prints) and,
somewhere in it, exactly one sentinel-framed JSON envelope produced by
``sandbox_shim.emit_result`` / ``emit_error``. This module extracts and validates
that envelope, and classifies the ways it can be absent or malformed so the runtime
can map a failure to ``ErrorClass.CONTRACT_VIOLATION`` with a precise reason.

Sentinels and protocol version are imported directly from ``sandbox_shim`` — the
same constants the producer uses — so producer and consumer cannot drift.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from . import sandbox_shim
from .sandbox_shim import PROTOCOL_VERSION, RESULT_BEGIN, RESULT_END

# Canonical filename the shim is copied to *inside the container*, and the import
# the synthesized script is instructed to use: ``from _wfgps_runtime import ...``.
SHIM_MODULE_NAME = "_wfgps_runtime"


def shim_source_path() -> Path:
    """Absolute path to the shim file the sandbox copies into the container."""
    return Path(sandbox_shim.__file__).resolve()


# One compiled matcher for the full BEGIN..END block (non-greedy, dot-matches-newline
# so multi-line payloads are captured whole).
_BLOCK_RE = re.compile(re.escape(RESULT_BEGIN) + r"(.*?)" + re.escape(RESULT_END), re.DOTALL)


class ContractStatus(str, Enum):
    OK = "ok"        # script delivered an answer
    ERROR = "error"  # script reported a structured failure (contract still satisfied)


class ContractViolation(str, Enum):
    """Why no usable envelope was recovered. Distinct reasons because they imply
    different recalc routes — a truncated block usually means a kill/timeout, while
    invalid JSON usually means the model emitted something it shouldn't have."""

    NO_RESULT_BLOCK = "no_result_block"       # script never called emit_* at all
    TRUNCATED_BLOCK = "truncated_block"        # BEGIN seen, END missing (killed mid-emit?)
    INVALID_JSON = "invalid_json"              # block present but not parseable JSON
    MALFORMED_ENVELOPE = "malformed_envelope"  # parsed, but wrong shape/version


class ContractResult(BaseModel):
    """Outcome of parsing one execution's stdout for its result envelope."""

    model_config = ConfigDict(frozen=True)

    found: bool = Field(..., description="A complete, valid envelope was recovered.")
    status: ContractStatus | None = None
    payload: dict | None = Field(default=None, description="Present when status == OK.")
    error_message: str | None = None
    error_kind: str | None = None
    error_details: object | None = None
    violation: ContractViolation | None = Field(
        default=None, description="Set when found is False; the reason it failed."
    )
    block_count: int = Field(default=0, description="How many complete blocks were seen.")
    protocol_version: int | None = None

    @property
    def ok(self) -> bool:
        """The strong success signal the sandbox/edges care about."""
        return self.found and self.status is ContractStatus.OK

    # --- constructors -------------------------------------------------- #

    @classmethod
    def _violation(cls, reason: ContractViolation, *, block_count: int = 0) -> "ContractResult":
        return cls(found=False, violation=reason, block_count=block_count)

    @classmethod
    def _from_envelope(cls, env: dict, *, block_count: int) -> "ContractResult":
        # Shape/version validation. The shim always writes a well-formed envelope, so
        # any failure here means corruption or a version skew, not a normal outcome.
        if env.get("v") != PROTOCOL_VERSION or "status" not in env:
            return cls._violation(ContractViolation.MALFORMED_ENVELOPE, block_count=block_count)
        try:
            status = ContractStatus(env["status"])
        except ValueError:
            return cls._violation(ContractViolation.MALFORMED_ENVELOPE, block_count=block_count)

        if status is ContractStatus.OK:
            payload = env.get("payload")
            if not isinstance(payload, dict):
                return cls._violation(ContractViolation.MALFORMED_ENVELOPE, block_count=block_count)
            return cls(
                found=True, status=status, payload=payload,
                block_count=block_count, protocol_version=PROTOCOL_VERSION,
            )

        # status == ERROR: a satisfied contract carrying a structured failure.
        err = env.get("error") or {}
        return cls(
            found=True, status=status,
            error_message=err.get("message"),
            error_kind=err.get("kind"),
            error_details=err.get("details"),
            block_count=block_count, protocol_version=PROTOCOL_VERSION,
        )


def parse_stdout(stdout: str) -> ContractResult:
    """Recover the result envelope from raw container stdout.

    If multiple complete blocks are present (a misbehaving script that emitted more
    than once), the LAST one wins — it is the script's final word — and the count is
    preserved so the caller can flag the anomaly.
    """
    blocks = _BLOCK_RE.findall(stdout)
    if not blocks:
        # No complete block. Distinguish "never emitted" from "emitted but cut off",
        # because a dangling BEGIN strongly implies a timeout/OOM kill mid-write.
        if RESULT_BEGIN in stdout:
            return ContractResult._violation(ContractViolation.TRUNCATED_BLOCK)
        return ContractResult._violation(ContractViolation.NO_RESULT_BLOCK)

    raw = blocks[-1].strip()
    block_count = len(blocks)
    try:
        env = json.loads(raw)
    except json.JSONDecodeError:
        return ContractResult._violation(ContractViolation.INVALID_JSON, block_count=block_count)
    if not isinstance(env, dict):
        return ContractResult._violation(ContractViolation.MALFORMED_ENVELOPE, block_count=block_count)
    return ContractResult._from_envelope(env, block_count=block_count)


def strip_result_blocks(stdout: str) -> str:
    """Return stdout with all result blocks removed — i.e. just the diagnostics,
    suitable for rich logging without dumping the (possibly large) payload twice."""
    return _BLOCK_RE.sub("", stdout).strip()
