# ADR-0004: Desktop shell, live transport, and frontend framework

- Status: Accepted
- Date: 2026-07-01

## Context

The backend for a desktop gateway app already exists. `desktop/DesktopService` is
the local loopback boundary that exposes every screen as a frozen, secret-free
**view-model** with no execution path and no credential surface; the HTTP gateway
has a transport-agnostic `GatewayApp` with an ASGI binding (P0) and a minimal SPA.
`docs/ADAPTER_MATURITY.md` lists the two missing pieces as "Not implemented": the
**GUI** (Tauri/Electron/Qt) and the **loopback transport**.

Two gaps keep the current front end from feeling "lively":

1. There is no native desktop shell — only a static SPA served over HTTP.
2. `/v1/runs/{id}/events` returns a **snapshot** string, not a live push, so the
   timeline, approvals, incidents, and earnings do not update in real time.

We need to choose the shell, the real-time transport, and the frontend framework
so the product surface can be built against interfaces that are already done and
contract-tested.

## Decision

Build the desktop app as a **Tauri** shell with a **React + Vite** frontend,
driven by a **WebSocket** live transport, bound to the local-loopback
`DesktopService`.

- **Shell: Tauri** (Rust core + system webview). Chosen over Electron for a much
  smaller binary, a sandboxed-by-default security model, and a natural fit for
  binding to the local-loopback `DesktopService` over an IPC/localhost channel.
- **Frontend: React + Vite** (TypeScript). Chosen for its large component
  ecosystem, fast HMR dev loop, and first-class TypeScript support; the built
  bundle is embedded directly into the Tauri app (no separate static host for the
  desktop target).
- **Live transport: WebSocket** over the existing ASGI binding, replacing the
  snapshot `events` endpoint with a live push of audit/run events. The durable
  runtime's fixed-size, append-only audit stream (and the serializable `RunState`
  of ADR-0002) map cleanly onto an incremental event feed.
- **Binding:** the desktop app talks only to the loopback `DesktopService`
  view-models (secret-free, no execution path); the multi-tenant HTTP gateway
  remains the door for web/mobile clients. Both sit on the same durable backend.
- **Do not build; connect:** identity (OIDC IdP — JWKS already supported),
  payments (Stripe Connect — already wired), the LLM runtime (routing gateway),
  and artifact storage (S3/GCS — the remaining P0 adapter). Every one of these is
  a connector, not a build.

### Rich screens (all backed by shipped endpoints)

Run timeline / agent-activity view, approvals + incidents inbox, nodeplace
discovery, and an earnings/payout dashboard — mapping to `/v1/runs/*`,
`/v1/nodeplace`, `/v1/listings`, `/v1/earnings`, and `/v1/payout-accounts`.

## Consequences

- The "lively" upgrade is confined to the **shell + transport** layer; the auth,
  money, nodeplace, and run APIs underneath are already implemented and tested, so
  the risk is concentrated in one place.
- Replacing the snapshot `events` endpoint with a live WebSocket push is the single
  highest-leverage change and is additive (the snapshot can remain for polling
  fallback).
- React + Vite commits us to the React ecosystem and its build toolchain; the
  bundle-into-Tauri path keeps the desktop target self-contained.
- Nothing here changes the backend contracts; a future web/mobile client reuses
  the same gateway and transport.
