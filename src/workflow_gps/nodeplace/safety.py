from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..skills.models import ActionEvent
from ..worker.leases import TrustLevel
from ..worker.policy import IsolationPolicy

_READ_ONLY_OPS = frozenset(
    {"get", "list", "read", "fetch", "query", "describe", "preview", "status", "search"}
)


class SafetyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    safe: bool
    required_backend: str
    requires_approval: bool
    reserved_actions: list[str]
    violations: list[str]


class NodeSafetyGate:
    def __init__(self, policy: IsolationPolicy | None = None) -> None:
        self._policy = policy or IsolationPolicy()

    def review(
        self,
        actions: list[ActionEvent],
        *,
        backend: str = "docker",
        requires_approval: bool = True,
    ) -> SafetyReport:
        allowed = self._policy.allowed_backends(TrustLevel.UNTRUSTED_SYNTHESIZED)
        reserved = [a.id for a in actions if a.operation.lower() not in _READ_ONLY_OPS]
        violations: list[str] = []
        if backend not in allowed:
            violations.append(
                f"node backend {backend!r} is not isolated; a node must run on one of "
                f"{sorted(allowed)}"
            )
        if reserved and not requires_approval:
            violations.append("reserved (mutating) actions require approval")
        return SafetyReport(
            safe=not violations,
            required_backend=self._policy.required_backend(
                TrustLevel.UNTRUSTED_SYNTHESIZED
            ),
            requires_approval=requires_approval,
            reserved_actions=reserved,
            violations=violations,
        )
