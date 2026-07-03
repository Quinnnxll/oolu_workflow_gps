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

## Shell (`src-tauri/`)

A Tauri v2 window. On launch the Rust core picks a free loopback port, spawns
the `wfgps` sidecar as `wfgps desktop --port <port>`, waits for it to accept
connections, then injects `window.__OOLU_API__ = "http://127.0.0.1:<port>"` into
the webview so the packaged (proxy-less) frontend targets the sidecar directly.
The sidecar is killed on window exit.

```sh
cd src-tauri
cargo tauri icon icons/source.png   # generate the platform icon set
cargo tauri dev                     # runs the sidecar + Vite together
```

The sidecar binary must exist at `src-tauri/binaries/wfgps-<target-triple>` for a
packaged build (CI produces it with PyInstaller).

## Installer (`OoLu-Setup.exe`)

Built by `.github/workflows/desktop-windows.yml` on `windows-latest`
(workflow_dispatch or a `desktop-v*` tag):

1. PyInstaller bundles `wfgps` from `sidecar/wfgps.spec` into a single exe and
   names it `wfgps-x86_64-pc-windows-msvc.exe` (Tauri sidecar convention).
2. `npm ci` + `cargo tauri build --bundles nsis` emits the NSIS installer,
   uploaded as the `OoLu-Setup` artifact.

The installer is unsigned; Windows SmartScreen will warn on first run.
