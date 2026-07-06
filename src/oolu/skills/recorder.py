from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .models import ActionEvent, Demonstration, ExecutionStatus, StateSnapshot
from .ports import ObserverAdapter, StateProbe

# Backend audit events that mark an operation as unreliable or successful. Used to
# score a recording without inspecting any payload data.
_ERROR_EVENTS = frozenset(
    {
        "workflow.preflight_failed",
        "workflow.incident",
        "workflow.cancelled",
        "workflow.failed",
    }
)
_SUCCESS_EVENTS = frozenset({"workflow.executed", "workflow.completed"})


class LogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    at: datetime
    source: str
    event_type: str
    run_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LogSource(Protocol):
    def entries(self, *, since: datetime, until: datetime) -> list[LogEntry]: ...


class InMemoryLogSource:
    def __init__(self, entries: Iterable[LogEntry] | None = None):
        self._entries = list(entries or [])

    def add(self, entry: LogEntry) -> None:
        self._entries.append(entry)

    def entries(self, *, since: datetime, until: datetime) -> list[LogEntry]:
        return [e for e in self._entries if since <= e.at <= until]


class DurableAuditLogSource:
    """Adapts a durable audit log into the recorder's log timeline. Structural —
    it needs only ``records()`` returning entries with ``at``/``event_type``/
    ``run_id``/``seq`` — so ``skills`` never imports ``durable`` (no cycle). Only the
    event *shape* is captured (type/run/seq), never the payload data.
    """

    def __init__(self, audit: Any, *, source: str = "audit"):
        self._audit = audit
        self._source = source

    def entries(self, *, since: datetime, until: datetime) -> list[LogEntry]:
        return [
            LogEntry(
                at=r.at,
                source=self._source,
                event_type=r.event_type,
                run_id=r.run_id,
                detail={"seq": r.seq},
            )
            for r in self._audit.records()
            if since <= r.at <= until
        ]


class RecordingMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    duration_s: float
    action_count: int
    backend_event_count: int
    error_count: int
    succeeded: bool


class Recording(BaseModel):
    model_config = ConfigDict(frozen=True)

    demonstration: Demonstration
    metrics: RecordingMetrics
    timeline: list[dict] = Field(default_factory=list)


def _timeline(actions: list[ActionEvent], logs: list[LogEntry]) -> list[dict]:
    items: list[dict] = [
        {
            "kind": "action",
            "at": action.observed_at.isoformat(),
            "adapter": action.adapter,
            "operation": action.operation,
        }
        for action in actions
    ]
    items += [
        {
            "kind": "log",
            "at": log.at.isoformat(),
            "source": log.source,
            "event_type": log.event_type,
        }
        for log in logs
    ]
    items.sort(key=lambda item: item["at"])
    return items


def _metrics(
    started: datetime, ended: datetime, actions: list[ActionEvent], logs: list[LogEntry]
) -> RecordingMetrics:
    errors = sum(1 for log in logs if log.event_type in _ERROR_EVENTS)
    saw_success = any(log.event_type in _SUCCESS_EVENTS for log in logs)
    return RecordingMetrics(
        duration_s=max(0.0, (ended - started).total_seconds()),
        action_count=len(actions),
        backend_event_count=len(logs),
        error_count=errors,
        succeeded=errors == 0 and (saw_success or not logs),
    )


class DemonstrationRecorder:
    """Captures a GUI demonstration and the correlated backend system log over the
    same window, then folds both into one ``Demonstration`` the ``SkillLearner`` can
    consume. The GUI observer is pluggable (a browser/desktop ``ObserverAdapter``);
    the backend log comes from any ``LogSource`` (the durable audit stream in
    production). The correlated timeline + reliability/efficiency metrics let a later
    step pick the *best* way to operate (see :func:`select_best`).
    """

    def __init__(
        self,
        observer: ObserverAdapter,
        *,
        log_source: LogSource | None = None,
        probe: StateProbe | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._observer = observer
        self._log_source = log_source
        self._probe = probe
        self._clock = clock or (lambda: datetime.now(UTC))
        self._started: datetime | None = None
        self._before: StateSnapshot | None = None

    def start(self) -> None:
        self._started = self._clock()
        self._before = self._probe.capture() if self._probe else None

    def stop(
        self,
        *,
        intent: str,
        application: str | None = None,
        outcome: ExecutionStatus | None = None,
    ) -> Recording:
        if self._started is None:
            raise RuntimeError("recorder was not started")
        ended = self._clock()
        actions = list(self._observer.observe())
        logs = (
            self._log_source.entries(since=self._started, until=ended)
            if self._log_source
            else []
        )
        after = self._probe.capture() if self._probe else None
        timeline = _timeline(actions, logs)
        metrics = _metrics(self._started, ended, actions, logs)
        resolved_outcome = outcome or (
            ExecutionStatus.SUCCEEDED if metrics.succeeded else ExecutionStatus.FAILED
        )
        demonstration = Demonstration(
            intent=intent,
            actions=actions,
            before=self._before,
            after=after,
            outcome=resolved_outcome,
            evidence={
                "backend_log": [log.model_dump(mode="json") for log in logs],
                "metrics": metrics.model_dump(mode="json"),
                "timeline": timeline,
            },
            application=application,
        )
        self._started = None
        self._before = None
        return Recording(
            demonstration=demonstration, metrics=metrics, timeline=timeline
        )


def select_best(recordings: Iterable[Recording]) -> Recording | None:
    """The most efficient reliable variant: successful, fewest errors, then fastest,
    then fewest actions. ``None`` if no recording succeeded."""
    successful = [r for r in recordings if r.metrics.succeeded]
    if not successful:
        return None
    return min(
        successful,
        key=lambda r: (
            r.metrics.error_count,
            r.metrics.duration_s,
            r.metrics.action_count,
        ),
    )
