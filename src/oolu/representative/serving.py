"""Which model speaks for a user: the adapter-serving seam.

Phase 0 has no adapters — everyone shares the base model, and the Noop
default says so. Phase 1 replaces it with a vLLM-backed server that maps a
scope to its active LoRA (``user-{id}-v{n}``) and manages runtime loading.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AdapterServer(Protocol):
    """Port for per-user adapter routing on the inference server."""

    def model_for(self, scope: str) -> str | None:
        """The scope's model string, or None to use the shared base."""
        ...


class NoopAdapterServer:
    def model_for(self, scope: str) -> str | None:
        return None
