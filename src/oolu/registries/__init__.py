"""Protocol registries over the durable layer — stage R2.

R0 and R1 pinned the contracts; this package gives them addresses. Every
registry here rides the same ``DurableConnection`` as the rest of OoLu's
durable runtime, owns its own tables, and keeps the protocol's idioms:
refusals are named, history is never deleted, and time is injected.

What lives where:

* ``standards``: versioned standard editions with status — the plan's
  own fix for citation rot; a deployment asks the registry what is
  current instead of trusting prose;
* ``machines``: machine and sensor instances with status walls, and
  instrument selection by measurand, range, and uncertainty — never by
  machine name;
* ``methods``: the method-version lifecycle; only an approved version
  authorizes a test;
* ``workcells``: workcell versions, tools with wear and inspection
  state, fixtures, and coordinate-frame calibrations kept as history —
  plus the live frame-graph digest and the release checks that block a
  job on a worn or failed tool;
* ``testjobs``: the §9 authorization digest — an unauthorized method
  version quarantines the job at birth;
* ``rights_store``: R1's rights contract made durable and revocable,
  closing the seam promised there.
"""

from .machines import MachineInstance, MachineRegistry, SensorInstance
from .methods import MethodRegistry, MethodStatus
from .rights_store import RightsStore
from .standards import StandardEdition, StandardsRegistry
from .testjobs import AuthorizedTestJob, AuthorizedTestJobRegistry, authorization_digest
from .workcells import WorkcellRegistry

__all__ = [
    "AuthorizedTestJob",
    "MachineInstance",
    "MachineRegistry",
    "MethodRegistry",
    "MethodStatus",
    "RightsStore",
    "SensorInstance",
    "StandardEdition",
    "StandardsRegistry",
    "AuthorizedTestJobRegistry",
    "WorkcellRegistry",
    "authorization_digest",
]
