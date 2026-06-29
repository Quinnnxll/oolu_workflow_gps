"""The versioned OpenAPI description served at ``/v1/openapi.json``.

A compact but real contract document covering every resource the gateway exposes
(contracts/runs, questions, routes, approvals, incidents, provider connections, and
feedback). The API version lives both in the ``/v1`` path prefix and in ``info``.
"""

from __future__ import annotations

API_VERSION = "v1"


def build_openapi() -> dict:
    def op(summary: str, *, secured: bool = True) -> dict:
        entry: dict = {"summary": summary, "responses": {"200": {"description": "OK"}}}
        if secured:
            entry["security"] = [{"oidcBearer": []}]
        return entry

    paths = {
        "/v1/runs": {
            "post": op("Submit a task contract (async; returns 202 with a run id)"),
            "get": op("List runs for the caller's tenant (paginated)"),
        },
        "/v1/runs/{run_id}": {"get": op("Get a run's status")},
        "/v1/runs/{run_id}/questions": {"get": op("List clarification questions")},
        "/v1/runs/{run_id}/answers": {"post": op("Answer clarification questions")},
        "/v1/runs/{run_id}/route": {"get": op("Preview the chosen route")},
        "/v1/runs/{run_id}/confirmation": {"post": op("Confirm or decline a route")},
        "/v1/runs/{run_id}/approvals": {
            "get": op("List approval state"),
            "post": op("Approve a reserved route (requires approver authority)"),
        },
        "/v1/runs/{run_id}/incidents": {
            "get": op("List incidents"),
            "post": op("Resolve an incident (retry or abort)"),
        },
        "/v1/runs/{run_id}/cancel": {"post": op("Cancel a run")},
        "/v1/runs/{run_id}/feedback": {"post": op("Submit feedback for a run")},
        "/v1/runs/{run_id}/audit": {"get": op("Export the verifiable audit trail")},
        "/v1/runs/{run_id}/events": {"get": op("Server-sent event stream of progress")},
        "/v1/provider-connections": {
            "get": op("List provider connections"),
            "post": op("Connect a provider (requires providers:manage)"),
        },
        "/v1/metrics": {"get": op("Operational metrics")},
        "/v1/openapi.json": {"get": op("This document", secured=False)},
    }

    return {
        "openapi": "3.1.0",
        "info": {"title": "Workflow-GPS Gateway", "version": API_VERSION},
        "servers": [{"url": "/"}],
        "components": {
            "securitySchemes": {
                "oidcBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            }
        },
        "paths": paths,
    }
