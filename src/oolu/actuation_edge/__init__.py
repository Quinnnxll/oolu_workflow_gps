"""Edge, compiler, and the qualified-workcell semantics — stage R4.

The hardware-facing stage, built hardware-honest: everything a real
cell will run is here as deterministic logic with the physical seams
injected (a plant callable for actuals, a cloud link flag, a safety
controller that answers locally). What this package pins:

* ``program_ir``: the vendor-neutral program intermediate
  representation — typed, bounded steps about workpieces and tools,
  with the raw-command wall enforced again at this layer;
* ``compiler``: the deterministic compiler (plan review gap 2.2.4) —
  an eligible release verdict in, a byte-stable signed program artifact
  out; same plan, same dialect, same bytes, same digest, forever, and
  no door that accepts anything but a typed plan;
* ``jobs``: the binder from an R3 release + compiled program into the
  R0 ``ActuationJob`` whose aggregate digest arms;
* ``leases``: R0's single-use lease semantics made durable — a spent
  lease stays spent across restarts;
* ``gateway``: program-artifact verification before arming, and
  command/evidence channel separation as distinct identities;
* ``execution``: the local execution contract — cloud loss changes
  nothing locally, lease expiry blocks the next start but never stops
  a cycle, commanded-versus-actual deviation beyond its bound causes a
  local hold with immutable evidence, safety trips never auto-restart,
  and a first piece cannot become a batch without inspection approval.
"""

from .compiler import CompileRefused, MachineProgram, PilotDialect, compile_program
from .execution import (
    BatchGate,
    DeviationMonitor,
    ExecutionMode,
    LocalExecutor,
    SafetyControllerSim,
)
from .gateway import ActuationGateway, ChannelIdentities
from .jobs import bind_actuation_job
from .leases import DurableLeaseService
from .program_ir import IROp, IRStep, ProgramIR

__all__ = [
    "ActuationGateway",
    "BatchGate",
    "ChannelIdentities",
    "CompileRefused",
    "DeviationMonitor",
    "DurableLeaseService",
    "ExecutionMode",
    "IROp",
    "IRStep",
    "LocalExecutor",
    "MachineProgram",
    "PilotDialect",
    "ProgramIR",
    "SafetyControllerSim",
    "bind_actuation_job",
    "compile_program",
]
