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
        "/v1/chat": {
            "post": op(
                "One conversational turn with the OoLu assistant: replies "
                "in kind, and when the message is work, starts a run and "
                "returns its id alongside the reply"
            ),
        },
        "/v1/runs": {
            "post": op(
                "Submit a task contract (async; returns 202 with a run id). "
                "An optional node_version_id binds the run to a marketplace "
                "node: the price clears and the noder shares attach, and "
                "earnings accrue only on platform-verified success"
            ),
            "get": op("List runs for the caller's tenant (paginated)"),
        },
        "/v1/runs/contract": {
            "post": op(
                "Execute an assembled contract directly: compiles to a DAG, "
                "binds every marketplace node (aggregate lineage-weighted "
                "shares), runs it, and audit-links the outcome. A contract "
                "with reserved actions is HELD (202 awaiting_approval), "
                "tenant-scoped and durable, for an authorized decision. "
                "Budget-gated: a 'budget' hard_cap refuses (402), a "
                "review_threshold, the caller's own spending behavior, or a "
                "partial linked wallet hold the run (409) until "
                "'review_acknowledged: true'"
            ),
        },
        "/v1/runs/contract/holds": {
            "get": op("List the caller tenant's held reserved contracts"),
        },
        "/v1/runs/contract/holds/events": {
            "get": op(
                "SSE snapshot of the tenant's hold lifecycle "
                "(held/approved/declined/expired) — the approver's feed; "
                "'?after=<seq>' resumes past frames already seen"
            ),
        },
        "/v1/runs/contract/holds/{pending_id}": {
            "post": op(
                "Decide a held reserved contract (requires approver "
                "authority): approval re-runs the budget gate on the "
                "submitter's terms and executes with the run bound to the "
                "ORIGINAL submitter; declining removes it. Both audited"
            ),
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
        "/v1/files": {
            "get": op("List the caller's files (documents and sheets)"),
            "post": op("Create a file in the durable database"),
        },
        "/v1/files/{file_id}": {
            "get": op("Read a file, content included"),
            "put": op("Edit a file's name and/or content"),
            "delete": op("Delete a file"),
        },
        "/v1/work/nodes": {
            "get": op(
                "The Work environment's node account list: every node the "
                "caller answers for, with account (responsible, admin, "
                "authority level, status, audit mode), cumulative earnings, "
                "and platform-verified health"
            ),
        },
        "/v1/work/nodes/{node_id}/account": {
            "post": op(
                "Create or update a node's account; a node with no account "
                "yet is onboarded — the caller becomes its responsible"
            ),
        },
        "/v1/work/nodes/{node_id}/activity": {
            "get": op(
                "The node's execution feed: runs bound to its versions, "
                "expanded into audit steps"
            ),
        },
        "/v1/nodeplace": {
            "get": op("List the caller's own contributed nodes"),
            "post": op(
                "Contribute a workflow as a node (opt-in, sanitized; "
                "derived_from records royalty lineage)"
            ),
        },
        "/v1/nodeplace/{node_id}/revoke": {"post": op("Revoke a contributed node")},
        "/v1/listings": {"get": op("Discover active public node listings")},
        "/v1/listings/{listing_id}/publish": {"post": op("Publish a draft listing")},
        "/v1/versions/{version_id}/ratings": {
            "get": op("List a version's ratings and reputation"),
            "post": op("Rate a version (requires a verified successful run)"),
        },
        "/v1/market/candidates": {
            "get": op(
                "Rank live candidates by verified quality per retry-adjusted "
                "dollar (read-only price preview)"
            ),
        },
        "/v1/market/quotes": {
            "post": op(
                "Quote a workflow from live economics (forecast; no money moves)"
            ),
        },
        "/v1/market/assemble": {
            "post": op(
                "Assemble a workflow from a goal's wanted slots via the "
                "marketplace's slot vocabularies (planning preview with "
                "lineage-aware payout previews; read-only). Picks are "
                "greedy-by-posterior by default; 'explore: true' "
                "Thompson-samples them from the caller's own run history; "
                "a positive 'cost_weight' ranks by expected utility "
                "(quality minus weighted cost) instead of quality alone"
            ),
        },
        "/v1/earnings": {"get": op("The caller's own earnings balance")},
        "/v1/earnings/entries": {"get": op("The caller's own earnings ledger entries")},
        "/v1/payout-accounts": {
            "get": op("The caller's payout account and KYC status"),
            "post": op("Onboard a payout (Stripe Connect) account for the caller"),
        },
        "/v1/disputes/{event_id}": {"get": op("List disputes for an event")},
        "/v1/webhooks/processor": {
            "post": op(
                "Payment-processor webhook (HMAC-signed, replay-protected)",
                secured=False,
            )
        },
        "/v1/openapi.json": {"get": op("This document", secured=False)},
    }

    return {
        "openapi": "3.1.0",
        "info": {"title": "OoLu Gateway", "version": API_VERSION},
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
