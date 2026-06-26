"""Telemetry — rich console logging, run summaries, and token/latency metrics."""

from __future__ import annotations

from .logging import (
    MetricsCollector,
    RunMetrics,
    configure_logging,
    get_logger,
    render_result,
)

__all__ = [
    "configure_logging",
    "get_logger",
    "render_result",
    "MetricsCollector",
    "RunMetrics",
]
