# OoLu Desktop (ADR-0004)

The local single-user desktop shell: a Tauri window over a React/Vite frontend
that binds to the Python `DesktopLoopbackApp` on `127.0.0.1`.

```
desktop-app/
  frontend/      React + Vite UI (this increment)
  src-tauri/     Rust shell + Python sidecar wiring (next increment)
```

## Boundary

The UI only ever talks to the loopback transport
(`workflow_gps.desktop.loopback.DesktopLoopbackApp`), which serves secret-free
view-models with **no auth and no execution path**. The multi-tenant OIDC
gateway remains the door for web/mobile. Never bind the loopback to a
non-loopback interface.

## Frontend scope

Task entry → guided clarification → confirmation/incident decisions →
live timeline (WebSocket), an **Inbox** of items waiting on the user, and a
searchable **Skills** library. Approvals are identity-gated and stay in the
gateway, not here.

## Develop

```sh
cd frontend
npm install
OOLU_LOOPBACK=http://127.0.0.1:8765 npm run dev
```

Vite proxies `/v1/*` (HTTP + WebSocket) to the loopback backend. Serve it with:

```sh
wfgps desktop --port 8765 --registry .workflow-gps/skills.db --seed-starter
```

`wfgps desktop` refuses any non-loopback `--host`.

## Build

```sh
cd frontend && npm run build   # → frontend/dist, embedded by the Tauri shell
```
