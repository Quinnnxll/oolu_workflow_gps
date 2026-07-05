"""Isolation policy: which backends may run a task at a given trust level.

Untrusted synthesized code may only run in an isolated backend (Docker or a
stronger restricted worker); the subprocess backend is reserved for explicitly
trusted local skills. The control plane stamps the required backend onto the lease,
and the worker enforces the allowed set before executing — so a worker cannot be
tricked into running untrusted code on the host.
"""

from __future__ import annotations

from .errors import IsolationViolation
from .leases import TrustLevel

# Backends that provide real isolation for untrusted code.
_ISOLATED_BACKENDS = frozenset({"docker", "restricted-worker"})


class IsolationPolicy:
    def __init__(
        self,
        *,
        isolated_backends: frozenset[str] = _ISOLATED_BACKENDS,
        trusted_local_backends: frozenset[str] = frozenset(
            {"subprocess", "docker", "restricted-worker"}
        ),
    ):
        self._isolated = isolated_backends
        self._trusted_local = trusted_local_backends

    def allowed_backends(self, trust: TrustLevel) -> frozenset[str]:
        if trust is TrustLevel.UNTRUSTED_SYNTHESIZED:
            return self._isolated
        return self._trusted_local

    def required_backend(self, trust: TrustLevel) -> str:
        # The minimum a lease must demand: Docker for untrusted, subprocess for trusted.
        if trust is TrustLevel.UNTRUSTED_SYNTHESIZED:
            return "docker"
        return "subprocess"

    def enforce(self, trust: TrustLevel, backend_kind: str) -> None:
        if backend_kind not in self.allowed_backends(trust):
            raise IsolationViolation(
                f"{trust.value} may not run on backend {backend_kind!r}; "
                f"allowed: {sorted(self.allowed_backends(trust))}"
            )


def execution_labels(policy: IsolationPolicy) -> list[dict]:
    """Human-scale labels for what may run where, per trust level.

    The one description both shells (loopback and unified gateway)
    render on their health screens — computed from the policy that is
    actually enforced, never restated by hand.
    """
    labels: list[dict] = []
    for trust in (TrustLevel.UNTRUSTED_SYNTHESIZED, TrustLevel.TRUSTED_LOCAL_SKILL):
        allowed = sorted(policy.allowed_backends(trust))
        labels.append(
            {
                "trust_level": trust.value,
                "allowed_backends": allowed,
                "isolated": "subprocess" not in allowed,
                "label": (
                    "Untrusted — isolated container only"
                    if trust is TrustLevel.UNTRUSTED_SYNTHESIZED
                    else "Trusted local skill — may run on host"
                ),
            }
        )
    return labels
