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
(`oolu.desktop.loopback.DesktopLoopbackApp`), which serves secret-free
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
oolu desktop --port 8765 --registry .oolu/skills.db --seed-starter
```

`oolu desktop` refuses any non-loopback `--host`.

## Test

```sh
cd frontend && npm run test    # vitest (jsdom): api adapter, WS mapping, login
```

The suite pins the client to the real gateway contract — `/v1/runs/*` routes,
run-view composition, the `/v1/runs` → inbox derivation, `/v1/listings` for
skills, audit-frame mapping over the bearer WebSocket, and the 401 → sign-out
path — so a drift in either side fails fast.

## Build

```sh
cd frontend && npm run build   # → frontend/dist, embedded by the Tauri shell
```

## Shell (`src-tauri/`)

A Tauri v2 window. On launch the Rust core picks a free loopback port, spawns
the `oolu` sidecar as `oolu desktop --port <port>`, waits for it to accept
connections, then injects `window.__OOLU_API__ = "http://127.0.0.1:<port>"` into
the webview so the packaged (proxy-less) frontend targets the sidecar directly.
The sidecar is killed on window exit.

```sh
cd src-tauri
cargo tauri icon icons/source.png   # generate the platform icon set
cargo tauri dev                     # runs the sidecar + Vite together
```

The sidecar binary must exist at `src-tauri/binaries/oolu-<target-triple>` for a
packaged build (CI produces it with PyInstaller).

## Local vs. remote (online host) mode

The shell has two modes, chosen at **build time** — never by the end user:

- **Local (default):** `OOLU_SERVER_URL` unset. The Rust core spawns the `oolu`
  loopback sidecar and injects its origin — the offline/solo experience above.
- **Remote:** compile with `OOLU_SERVER_URL=https://<your-host>`. No sidecar is
  spawned; the window injects that URL as `window.__OOLU_API__` and sets
  `window.__OOLU_REMOTE__ = true`, so the frontend shows a sign-in screen,
  posts to `/v1/auth/login`, and carries the bearer token (persisted in
  `localStorage`, so sign-in survives restarts) on every request and on the
  event WebSocket (via the `bearer, <token>` subprotocol).

Run the matching server with `oolu host --database-url <postgres-dsn>
--allow-origin <this app's origin>` behind HTTPS (see the top-level README).

### Later setup (once the online domain exists)

The domain is intentionally **not wired yet**. To turn on remote mode later,
two build-time values must be set together — there is no runtime/user setting:

1. Build with `OOLU_SERVER_URL=https://<your-host>` (e.g. exported in the
   `desktop-windows.yml` build step).
2. Add that origin to the webview CSP `connect-src` in
   `src-tauri/tauri.conf.json` (today it is loopback-only):
   `https://<your-host> wss://<your-host>`.

## Installer (`OoLu-Setup.exe`)

Built by `.github/workflows/desktop-windows.yml` on `windows-latest`
(workflow_dispatch or a `desktop-v*` tag):

1. PyInstaller bundles `oolu` from `sidecar/oolu.spec` into a single exe and
   names it `oolu-x86_64-pc-windows-msvc.exe` (Tauri sidecar convention).
2. `npm ci` + `cargo tauri build --bundles nsis` emits the NSIS installer,
   uploaded as the `OoLu-Setup` artifact.

The installer is unsigned; Windows SmartScreen will warn on first run.
