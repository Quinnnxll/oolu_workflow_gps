"""Worker control plane: planning/dispatch separated from privileged execution.

Signed, expiring, audience-bound, single-use task leases authorize execution; the
control plane holds no backend and no credentials; workers run code under an
isolation policy (untrusted code requires Docker or stronger); and outbound-only
local agents serve desktop/private-network resources without exposing an inbound
port or surrendering local credentials. See ``docs/ADAPTER_MATURITY.md``.
"""

from .control_plane import (
    Assignment,
    ControlPlane,
    TaskRequest,
    WorkerInfo,
    WorkerKind,
)
from .errors import (
    AudienceMismatch,
    CapacityExceeded,
    ExpiredLease,
    InvalidLease,
    IsolationViolation,
    LeaseError,
    ReplayedLease,
    RevokedLease,
    TaskTimeout,
    WorkerError,
    WorkerQuarantined,
)
from .leases import (
    LeaseSigner,
    LeaseVerifier,
    TrustLevel,
    WorkerTaskLease,
)
from .ledger import (
    WORKER_MIGRATIONS,
    InMemoryLeaseLedger,
    LeaseLedger,
    LocalLeaseLedger,
)
from .local_agent import LocalAgent, LocalSecretProvider
from .policy import IsolationPolicy
from .worker import StubWorkerExecutor, Worker, WorkerExecutor

__all__ = [
    "WORKER_MIGRATIONS",
    "Assignment",
    "AudienceMismatch",
    "CapacityExceeded",
    "ControlPlane",
    "ExpiredLease",
    "InMemoryLeaseLedger",
    "InvalidLease",
    "IsolationPolicy",
    "IsolationViolation",
    "LeaseError",
    "LeaseLedger",
    "LeaseSigner",
    "LeaseVerifier",
    "LocalAgent",
    "LocalLeaseLedger",
    "LocalSecretProvider",
    "ReplayedLease",
    "RevokedLease",
    "StubWorkerExecutor",
    "TaskRequest",
    "TaskTimeout",
    "TrustLevel",
    "Worker",
    "WorkerError",
    "WorkerExecutor",
    "WorkerInfo",
    "WorkerKind",
    "WorkerQuarantined",
    "WorkerTaskLease",
]
