"""Telemetry — rich console logging, run summaries, and token/latency metrics.

Three concerns, all degrading gracefully when ``rich`` is absent:

  * ``configure_logging`` installs a rich handler on the ``workflow_gps`` logger so the
    stdlib ``logger.info(...)`` calls already sprinkled through the nodes and backends
    render as colored, aligned console output. Idempotent — safe to call repeatedly.

  * ``render_result`` prints a ``WorkflowResult`` as a tidy panel (success/failure,
    answer, recalc count, tier, escalations).

  * ``MetricsCollector`` aggregates token usage and latency across the gateway and
    backend calls of a run — the "monitor token usage metrics" goal. It is duck-typed
    (reads attributes off whatever ``SynthesisResult`` / ``ExecutionResult`` it is
    given) so telemetry stays a leaf module with no imports from routing/runtime/graph,
    and therefore no risk of an import cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_LOGGER_NAME = "workflow_gps"
_HANDLER_FLAG = "_wfgps_handler"


# --------------------------------------------------------------------------- #
# Logging setup.                                                              #
# --------------------------------------------------------------------------- #
def configure_logging(
    *,
    level: int | str = "INFO",
    show_path: bool = False,
    rich_tracebacks: bool = True,
) -> logging.Logger:
    """Install a rich console handler on the package logger. Idempotent.

    Falls back to a plain stdlib handler if ``rich`` is not installed, so logging
    always works; rich just makes it prettier.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)

    # Remove any handler we previously attached, so repeated calls don't stack.
    for handler in [h for h in logger.handlers if getattr(h, _HANDLER_FLAG, False)]:
        logger.removeHandler(handler)

    handler = _make_handler(show_path=show_path, rich_tracebacks=rich_tracebacks)
    setattr(handler, _HANDLER_FLAG, True)
    logger.addHandler(handler)
    logger.propagate = False  # don't double-log through the root logger
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """A logger under the package namespace, e.g. get_logger('runtime')."""
    return logging.getLogger(_LOGGER_NAME if not name else f"{_LOGGER_NAME}.{name}")


def _make_handler(*, show_path: bool, rich_tracebacks: bool) -> logging.Handler:
    try:
        from rich.console import Console
        from rich.logging import RichHandler

        return RichHandler(
            console=Console(stderr=True),
            show_path=show_path,
            rich_tracebacks=rich_tracebacks,
            markup=False,
            log_time_format="[%H:%M:%S]",
        )
    except ImportError:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
        return handler


# --------------------------------------------------------------------------- #
# Run summary.                                                                 #
# --------------------------------------------------------------------------- #
def render_result(result, *, console=None) -> None:
    """Print a ``WorkflowResult`` (duck-typed) as a panel. Plain-text fallback."""
    success = getattr(result, "success", False)
    status = _enum_value(getattr(result, "status", "?"))
    tier = _enum_value(getattr(result, "final_tier", "?"))
    answer = getattr(result, "answer", None)
    reason = getattr(result, "failure_reason", None)
    recalcs = getattr(result, "recalc_count", 0)
    escalations = getattr(result, "tier_escalations", 0)
    attempts = getattr(result, "attempts", 0)

    rows = [
        ("status", status),
        ("attempts", str(attempts)),
        ("recalc cycles", str(recalcs)),
        ("tier escalations", str(escalations)),
        ("final tier", tier),
    ]
    rows.append(("answer", _short(answer)) if success else ("failure", _short(reason)))

    metrics = getattr(result, "metrics", None)
    if metrics is not None and getattr(metrics, "gateway_calls", 0):
        rows.append(("tokens (p+c)",
                     f"{metrics.prompt_tokens}+{metrics.completion_tokens}={metrics.total_tokens}"))
        rows.append(("model / sandbox time",
                     f"{metrics.gateway_seconds:.2f}s / {metrics.backend_seconds:.2f}s"))

    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", style="bold")
        table.add_column()
        for key, val in rows:
            table.add_row(key, val)
        title = "[green]✓ completed[/green]" if success else "[red]✗ halted[/red]"
        (console or Console()).print(Panel(table, title=f"Workflow-GPS · {title}", expand=False))
    except ImportError:
        line = " | ".join(f"{k}={v}" for k, v in rows)
        print(f"Workflow-GPS [{'OK' if success else 'FAIL'}] {line}")


# --------------------------------------------------------------------------- #
# Token / latency metrics.                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class RunMetrics:
    """A point-in-time snapshot of accumulated usage for a run."""

    gateway_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    gateway_seconds: float = 0.0
    backend_calls: int = 0
    backend_seconds: float = 0.0


@dataclass
class MetricsCollector:
    """Accumulate token usage and latency across a run. Duck-typed inputs."""

    _m: RunMetrics = field(default_factory=RunMetrics)

    def record_synthesis(self, result) -> None:
        """Feed a SynthesisResult (or anything with the same attributes)."""
        self._m.gateway_calls += 1
        self._m.prompt_tokens += int(getattr(result, "prompt_tokens", 0) or 0)
        self._m.completion_tokens += int(getattr(result, "completion_tokens", 0) or 0)
        self._m.total_tokens += int(getattr(result, "total_tokens", 0) or 0)
        self._m.gateway_seconds += float(getattr(result, "duration_s", 0.0) or 0.0)

    def record_execution(self, result) -> None:
        """Feed an ExecutionResult (or anything with a duration_s)."""
        self._m.backend_calls += 1
        self._m.backend_seconds += float(getattr(result, "duration_s", 0.0) or 0.0)

    def snapshot(self) -> RunMetrics:
        return RunMetrics(**vars(self._m))

    def render(self, *, console=None) -> None:
        m = self._m
        rows = [
            ("gateway calls", str(m.gateway_calls)),
            ("prompt / completion tokens", f"{m.prompt_tokens} / {m.completion_tokens}"),
            ("total tokens", str(m.total_tokens)),
            ("gateway time", f"{m.gateway_seconds:.2f}s"),
            ("backend calls", str(m.backend_calls)),
            ("backend time", f"{m.backend_seconds:.2f}s"),
        ]
        try:
            from rich.console import Console
            from rich.table import Table

            table = Table(title="run metrics", show_header=False, expand=False)
            table.add_column(justify="right", style="bold")
            table.add_column()
            for key, val in rows:
                table.add_row(key, val)
            (console or Console()).print(table)
        except ImportError:
            print("run metrics: " + " | ".join(f"{k}={v}" for k, v in rows))


# --------------------------------------------------------------------------- #
# Small helpers.                                                               #
# --------------------------------------------------------------------------- #
def _enum_value(value) -> str:
    return getattr(value, "value", str(value))


def _short(value, limit: int = 200) -> str:
    text = "—" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
