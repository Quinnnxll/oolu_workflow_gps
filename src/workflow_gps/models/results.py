"""Execution results and the two-phase lifecycle marker.

This is what the sandbox hands back after running a synthesized script inside an
isolated container. It deliberately separates three notions that are easy to
conflate:

  * the process *ran* (we have an exit code at all),
  * the process *succeeded* (exit 0, no timeout),
  * we got a *usable answer* (a valid sentinel-delimited JSON payload — trap #7).

A script can exit 0 and still have produced nothing parseable; that is a
``CONTRACT_VIOLATION``, not a success. ``succeeded`` encodes the full bar.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .errors import ErrorRecord


class Phase(str, Enum):
    """The two halves of every hostile execution.

    Phase A and Phase B exist to resolve the standoff between 'hostile by default'
    (no network) and 'auto-install dependencies' (needs network). Network is opened
    only for installs, against a pinned index, then severed before any synthesized
    code runs.
    """

    INSTALL = "install"   # Phase A: network-enabled, pinned index ONLY
    EXECUTE = "execute"   # Phase B: network fully severed


class ExecutionResult(BaseModel):
    """Outcome of one sandbox run. Frozen — it is a record of what happened."""

    model_config = ConfigDict(frozen=True)

    phase: Phase = Field(..., description="Which lifecycle phase produced this result.")
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False

    # The runtime<->script contract. `contract_payload` is the parsed sentinel JSON
    # block; everything else in stdout is treated as diagnostic noise.
    contract_ok: bool = Field(default=False, description="A valid sentinel JSON block was parsed.")
    contract_payload: dict | None = Field(default=None, description="Parsed result block, if any.")

    # Populated by the classifier when the run failed. None on a clean run.
    error: ErrorRecord | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def succeeded(self) -> bool:
        """Full success bar: clean exit, no timeout, a usable answer, no classified error."""
        return (
            self.exit_code == 0
            and not self.timed_out
            and self.contract_ok
            and self.error is None
        )
