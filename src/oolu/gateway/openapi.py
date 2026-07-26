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
        "/v1/api-keys": {
            "get": op("List the caller tenant's API keys (never secrets)"),
            "post": op(
                "Issue a machine key for the public execution API; the "
                "secret appears only in this response. Scopes: runs:submit, "
                "runs:read, market:read"
            ),
        },
        "/v1/api-keys/{key_id}": {"delete": op("Revoke an API key")},
        "/v1/webhook-endpoints": {
            "get": op("List the tenant's run-event webhook endpoints"),
            "post": op(
                "Register a webhook URL for terminal run events; the "
                "signing secret appears only in this response"
            ),
        },
        "/v1/webhook-endpoints/{endpoint_id}": {
            "delete": op("Remove a webhook endpoint"),
        },
        "/v1/payment-methods": {
            "get": op("The caller's saved cards (metadata only) and default"),
            "post": op(
                "Save a card. Pre-launch: named TEST cards only — no field "
                "carries a real number; live mode will use client-confirmed "
                "SetupIntents"
            ),
        },
        "/v1/payment-methods/{pm_ref}": {
            "delete": op("Remove a saved card"),
        },
        "/v1/payment-methods/{pm_ref}/default": {
            "post": op("Make a saved card the default"),
        },
        "/v1/payments/status": {
            "get": op(
                "Is real charging open? The launch guard's doors spelled "
                "out: transaction port, price settlement, verification"
            ),
        },
        "/v1/subscription": {
            "get": op(
                "The plan lifecycle: current plan, cycle, period, credit, "
                "and the price table"
            ),
        },
        "/v1/subscription/choose": {
            "post": op(
                "Start a plan (only from free — cancel first to change "
                "terms); any credit is deducted from the price"
            ),
        },
        "/v1/subscription/cancel": {
            "post": op(
                "End the current plan now; unused time becomes credit"
            ),
        },
        "/v1/settings": {
            "get": op(
                "The settings node: every declared setting with its bounds "
                "and current value"
            ),
            "put": op(
                "Apply setting changes through the declared catalog only "
                "(unknown key or out-of-bounds value → 400)"
            ),
        },
        "/v1/client-config": {
            "get": op(
                "What a client should know before signing in: the paired "
                "online server and which sign-in doors exist (public)"
            ),
        },
        "/v1/auth/register": {
            "post": op(
                "Self-serve e-mail registration where the host opts in "
                "(--open-registration); the e-mail is linked so it cannot "
                "register twice (public)"
            ),
        },
        "/v1/auth/google/start": {
            "get": op(
                "Begin Sign in with Google: the consent URL + a one-shot "
                "state (public)"
            ),
        },
        "/v1/auth/google/callback": {
            "get": op(
                "Google's browser redirect lands here; the page never "
                "carries the session token (public)"
            ),
        },
        "/v1/auth/google/finish": {
            "post": op(
                "The app's poll: pending until the browser leg lands, "
                "then the session token exactly once (public)"
            ),
        },
        "/v1/auth/google/link": {
            "post": op(
                "Attach Google to the signed-in account — the local-mode "
                "upgrade path that keeps all local data"
            ),
        },
        "/v1/keys/model": {
            "get": op(
                "Configured model providers with key fingerprints — "
                "never the keys"
            ),
            "post": op(
                "Store a provider API key in the encrypted keyring; "
                "answers with a fingerprint only"
            ),
        },
        "/v1/keys/model/{provider}": {
            "delete": op("Remove a stored model key"),
        },
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
        "/v1/commerce/policy": {
            "get": op("The caller's purchase policy (typed limits, versioned)"),
            "put": op("Replace the caller's purchase policy"),
        },
        "/v1/commerce/delegations": {
            "get": op("The caller's agent delegations"),
            "post": op(
                "Grant an agent a typed, expiring, revocable commercial "
                "delegation (actions, categories, amounts, counterparties, "
                "destinations)"
            ),
        },
        "/v1/commerce/delegations/{delegation_id}": {
            "delete": op(
                "Revoke a delegation; every unexecuted intent the agent "
                "minted is blocked immediately"
            ),
        },
        "/v1/commerce/intents": {
            "get": op("The caller's commercial intents with states and verdicts"),
            "post": op(
                "Create an immutable purchase intent bound to an exact "
                "versioned offer; the deterministic policy ladder decides "
                "auto-execute / notify / approval / deny and the digest is "
                "minted (idempotent on idempotency_key; no money moves — "
                "no execution door exists yet)"
            ),
        },
        "/v1/commerce/intents/{intent_id}": {
            "get": op("One intent: exact terms, state, digest, and verdict"),
        },
        "/v1/commerce/approvals": {
            "get": op(
                "The commerce review inbox: exact-terms summaries of every "
                "intent awaiting a digest-bound approval"
            ),
        },
        "/v1/commerce/intents/{intent_id}/approval": {
            "post": op(
                "Approve or reject the exact digested terms (strong "
                "decisions demand step-up authentication; approvals are "
                "single-use and expire)"
            ),
        },
        "/v1/commerce/catalog": {
            "get": op("Browse active fixed-price listings"),
        },
        "/v1/commerce/listings": {
            "get": op("The caller's own listings"),
            "post": op("Draft a fixed-price listing"),
        },
        "/v1/commerce/listings/{listing_id}/publish": {
            "post": op(
                "Publish a listing (verified sellers only — an MVP "
                "boundary, refused otherwise)"
            ),
        },
        "/v1/commerce/listings/{listing_id}/offer": {
            "post": op(
                "Mint the exact versioned offer for a quantity — what a "
                "purchase intent binds to"
            ),
        },
        "/v1/commerce/orders": {
            "get": op("The caller's orders, as buyer or hosted seller"),
            "post": op(
                "Place the order for an authorized intent (exactly once "
                "per intent; the digest law re-runs against the live "
                "offer; payment authorizes now and captures only on "
                "acceptance)"
            ),
        },
        "/v1/commerce/orders/{order_id}": {
            "get": op("One order: terms, state, and payment references"),
        },
        "/v1/commerce/orders/{order_id}/ship": {
            "post": op("Mark the order shipped (tracking optional)"),
        },
        "/v1/commerce/orders/{order_id}/deliver": {
            "post": op("Record delivery (evidence reference optional)"),
        },
        "/v1/commerce/orders/{order_id}/accept": {
            "post": op(
                "Buyer acceptance: capture happens here — the take-rate "
                "fee split posts to the double-entry ledger"
            ),
        },
        "/v1/commerce/orders/{order_id}/cancel": {
            "post": op("Cancel before fulfillment; the authorization voids"),
        },
        "/v1/commerce/orders/{order_id}/refund": {
            "post": op(
                "Full refund: the provider reverses the charge and the "
                "ledger posts the capture's exact negation"
            ),
        },
        "/v1/commerce/orders/{order_id}/ledger": {
            "get": op("The order's balanced ledger transactions"),
        },
        "/v1/commerce/orders/{order_id}/evidence": {
            "post": op(
                "Attach late delivery evidence — the escrow exception's "
                "way out; capture into escrow follows"
            ),
        },
        "/v1/commerce/orders/{order_id}/invoice": {
            "get": op("The completed order's generated invoice"),
        },
        "/v1/commerce/rfqs": {
            "get": op("Live requests for quotes (what sellers browse)"),
            "post": op(
                "Open a request for quotes from a typed specification — "
                "required attributes are the eligibility bar every quote "
                "meets or is marked by"
            ),
        },
        "/v1/commerce/rfqs/{rfq_id}/quotes": {
            "get": op(
                "Normalized comparison: eligible quotes by landed total, "
                "substitutes after them with their gaps named"
            ),
            "post": op(
                "Submit a quote (the seller's signed sales policy gates "
                "it — below the absolute floor is refused without model "
                "discretion; specification eligibility is judged and "
                "stored before any policy evaluation)"
            ),
        },
        "/v1/commerce/rfqs/{rfq_id}/award": {
            "post": op(
                "Award one ELIGIBLE quote; returns its exact versioned "
                "offer for the intent door — discovery never bypasses "
                "the digest law"
            ),
        },
        "/v1/commerce/sales-policy": {
            "get": op("The caller's seller automation policy"),
            "put": op(
                "Replace the caller's seller automation policy (floors, "
                "auto-accept price, discount and quantity limits)"
            ),
        },
        "/v1/commerce/orders/{order_id}/milestones": {
            "get": op("The order's payment schedule and each tranche's state"),
        },
        "/v1/commerce/orders/{order_id}/milestones/{index}/deliver": {
            "post": op(
                "Deliver one milestone with evidence — the first evidenced "
                "delivery captures the whole total into escrow"
            ),
        },
        "/v1/commerce/orders/{order_id}/milestones/{index}/accept": {
            "post": op(
                "Buyer acceptance of one milestone: exactly its tranche "
                "leaves escrow; nothing of the siblings moves"
            ),
        },
        "/v1/commerce/orders/{order_id}/milestones/{index}/fail": {
            "post": op(
                "Declare a milestone failed: the order disputes and every "
                "unreleased tranche freezes in escrow"
            ),
        },
        "/v1/commerce/orders/{order_id}/refund-unreleased": {
            "post": op(
                "Resolution for a frozen milestone order: the unreleased "
                "remainder returns to the buyer; accepted tranches stand"
            ),
        },
        "/v1/commerce/orders/{order_id}/adjudicate": {
            "post": op(
                "The marketplace's dispute verdict (approve authority "
                "required): replacement, reject, full_refund, or "
                "partial_refund — each a deterministic posting"
            ),
        },
        "/v1/commerce/recurring": {
            "get": op("The caller's recurring obligations"),
            "post": op(
                "Found a recurring obligation from an AUTHORIZED recurring "
                "intent — no obligation exists without the full policy pass"
            ),
        },
        "/v1/commerce/recurring/{obligation_id}/renew": {
            "post": op(
                "One period's renewal: identical terms proceed as an "
                "auto-approved intent; any material change refuses and "
                "re-enters policy as a new intent"
            ),
        },
        "/v1/commerce/recurring/{obligation_id}": {
            "delete": op("Cancel the obligation; nothing renews after"),
        },
        "/v1/commerce/payout-changes": {
            "get": op("The tenant's payout-destination change requests"),
            "post": op(
                "Request a payout-destination change: always dual-control "
                "(two distinct strong approvers, self-approval refused) "
                "plus a delay window before it can apply"
            ),
        },
        "/v1/commerce/payout-changes/{request_id}/approve": {
            "post": op(
                "One strong approval (step-up required; the requester can "
                "never approve their own change)"
            ),
        },
        "/v1/commerce/payout-changes/{request_id}/apply": {
            "post": op(
                "Apply the approved change — refused until the protection "
                "window has elapsed"
            ),
        },
        "/v1/commerce/orders/{order_id}/jobs": {
            "post": op(
                "Dispatch a typed execution job carrying the order's "
                "APPROVED price — never an opening bid"
            ),
        },
        "/v1/commerce/jobs/{job_id}/ack": {
            "post": op(
                "The node acknowledges price and schedule; different terms "
                "invalidate the prior approval by the digest law"
            ),
        },
        "/v1/commerce/jobs/{job_id}/complete": {
            "post": op("Execution evidence comes home"),
        },
        "/v1/commerce/peers": {
            "get": op("Registered peer markets"),
            "post": op(
                "Register a peer market (operator authority) with its "
                "jurisdiction — the compliance gate crosses the federation "
                "boundary"
            ),
        },
        "/v1/commerce/peers/{peer_id}/state": {
            "post": op(
                "Suspend or reactivate a peer; suspension blocks new "
                "imports immediately"
            ),
        },
        "/v1/commerce/peers/{peer_id}/offers": {
            "post": op(
                "Import a peer's signed offer (the A2A wire contract): a "
                "tampered or unsigned offer never reaches the intent door; "
                "what survives is an ordinary typed offer under the same "
                "policy engine and ledger"
            ),
        },
        "/v1/commerce/source": {
            "get": op(
                "The sourcing sweep: one specification against the local "
                "shelf and every federated import — one normalized, "
                "eligibility-marked comparison"
            ),
        },
        "/v1/commerce/reconciliation": {
            "get": op("Reconciliation exceptions, each with its evidence trail"),
        },
        "/v1/commerce/reconciliation/sweep": {
            "post": op(
                "Reconcile finished orders: matched close, mismatched file "
                "exceptions, duplicate charges dispute themselves"
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
