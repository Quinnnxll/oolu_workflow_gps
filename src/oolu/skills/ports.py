"""Ports that keep the skill core independent of deployment and vendor SDKs."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import (
    ActionEvent,
    ApprovalRecord,
    ConstraintSpec,
    ExecutionOutcome,
    ReusableSkill,
    StateSnapshot,
)


@runtime_checkable
class ObserverAdapter(Protocol):
    name: str

    def capabilities(self) -> frozenset[str]: ...

    def observe(self) -> tuple[ActionEvent, ...]: ...


@runtime_checkable
class ActionExecutor(Protocol):
    name: str

    def capabilities(self) -> frozenset[str]: ...

    def execute(
        self, action: ActionEvent, *, idempotency_key: str
    ) -> ExecutionOutcome: ...

    def cancel(self, idempotency_key: str) -> None: ...


@runtime_checkable
class StateProbe(Protocol):
    name: str

    def capture(self) -> StateSnapshot: ...


@runtime_checkable
class ConstraintValidator(Protocol):
    name: str

    def validate(
        self,
        constraint: ConstraintSpec,
        *,
        before: StateSnapshot | None,
        after: StateSnapshot,
    ) -> ConstraintSpec: ...


@runtime_checkable
class SkillStore(Protocol):
    def save(self, skill: ReusableSkill) -> None: ...

    def get(self, skill_id: str) -> ReusableSkill | None: ...

    def list(self, *, limit: int = 100) -> list[ReusableSkill]: ...

    def delete(self, skill_id: str) -> bool: ...

    def close(self) -> None: ...


@runtime_checkable
class ExecutionStore(Protocol):
    def save(self, outcome: ExecutionOutcome) -> None: ...

    def get(self, skill_id: str, idempotency_key: str) -> ExecutionOutcome | None: ...

    def close(self) -> None: ...


@runtime_checkable
class ArtifactStore(Protocol):
    def put(self, artifact_id: str, content: bytes, *, media_type: str) -> str: ...

    def get(self, artifact_ref: str) -> bytes: ...


@runtime_checkable
class SecretProvider(Protocol):
    def resolve(self, credential_ref: str) -> str: ...


@runtime_checkable
class GatewayClient(Protocol):
    def lease_task(self, *, capabilities: frozenset[str]) -> dict[str, Any] | None: ...

    def acknowledge(self, task_id: str, outcome: ExecutionOutcome) -> None: ...


@runtime_checkable
class EventSink(Protocol):
    def append(self, event_type: str, payload: dict[str, Any]) -> None: ...


@runtime_checkable
class ApprovalProvider(Protocol):
    def request(self, *, policy: str, scope: dict[str, Any]) -> ApprovalRecord: ...
