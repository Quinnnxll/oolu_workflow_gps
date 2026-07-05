# Workflow-GPS

A real-time **navigation engine for local LLM agentic workflows**.

Workflow-GPS treats APIs, local files, and system tasks as a *road network*, a user's
intent as a *destination*, and the agent loop as a *real-time navigation engine* (think
Google Maps). When a step fails — a missing package, a runtime exception, environmental
drift — the engine doesn't give up: it captures the error, **"recalculates,"** alters its
route (e.g. auto-installs a dependency, re-synthesizes the code), and keeps driving toward
the goal.

## Quickstart — download → run (no development tools needed)

1. **Download** this repository as a ZIP (the green **Code** button → *Download ZIP*)
   and unzip it anywhere.
2. **Install Python 3.11+** if you don't have it: <https://www.python.org/downloads/>
   (on Windows, tick *“Add python.exe to PATH”* in the installer).
3. **Run the setup script** in the unzipped folder:
   - **Windows:** double-click `setup.bat`
   - **macOS / Linux:** open a terminal in the folder and run `./setup.sh`

That's the whole setup. The first run creates a private environment in `.venv`
inside the folder and installs everything into it (a few minutes); every run
after that starts instantly. Your browser opens the **Workflow-GPS shell** at
`http://127.0.0.1:8765` — submit tasks, assemble marketplace workflows with
budget verdicts, decide approvals from the inbox, browse the skill library,
and track earnings. All data stays on your machine (in `.workflow-gps/`);
press **Ctrl+C** in the setup window to stop, and re-run the script to start
again. Nothing is installed outside the folder — delete it and everything is
gone.

The full model engine (LLM synthesis, sandboxed execution) is optional and
configured separately — see **Installation** and **Configuration** below.

### If something goes wrong

Run the built-in check-up — it tests everything this machine needs and
prints the exact fix for anything missing:

```bash
wfgps doctor          # or: .venv/bin/python -m workflow_gps.cli doctor
```

The traps it catches (and `wfgps run` now catches up front, with the same
directions instead of a traceback):

- **`wfgps run` needs the model engine** — install it with
  `pip install "workflow-gps[engine]"` (`langgraph` + `litellm`).
- **No model server answering** — by default the engine talks to a *local*
  OpenAI-compatible server (vLLM / Ollama / LM Studio) at
  `http://localhost:8000/v1`. Start one there, or point at your own endpoint
  with `--config models.yaml` (see **Configuration**).
- **`OPENAI_API_KEY` is not set** — litellm requires it even for local
  servers; any value works for vLLM (e.g. `OPENAI_API_KEY=EMPTY`).
- **Running `python src/workflow_gps/cli.py` directly** doesn't work
  (relative imports need the package context) — use `wfgps …` after
  installing, or `python -m workflow_gps.cli …`.
- **A `.venv` without pip** (some stripped-down Python builds) — the setup
  scripts now bootstrap pip automatically via `ensurepip`.

### Native app (single-file executable)

Prefer a double-clickable app with no Python visible at all? Build one:

```bash
python packaging/build_installer.py
```

This produces `dist/WorkflowGPS-Shell` (`WorkflowGPS-Shell.exe` on Windows) —
a single self-contained file you can copy anywhere and double-click. It starts
the same shell, opens your browser, and keeps its data in `~/.workflow-gps`.
PyInstaller cannot cross-compile, so build on each platform you target; the
`build-installers` GitHub Actions workflow builds all three (Windows, macOS,
Linux) on every version tag and attaches them as downloadable artifacts.

## Core ideas

- **"Recalculating…" self-healing.** Failures are normal outcomes, not dead ends. The
  engine classifies each failure and routes it through a recalculation loop (install a
  dependency, bump temperature, escalate the model tier) until it succeeds or hits a
  deliberate ceiling.
- **Code-as-an-interface (10× token efficiency).** Instead of multi-turn JSON tool
  thrashing, the model synthesizes a single self-contained Python script that runs locally
  in one turn.
- **Prefix-cache optimization.** Prompts are assembled deterministically with volatile
  values (timestamps, session IDs, iteration counters) pushed to the very end, maximizing
  prefix-cache reuse on the model backend.
- **Hostile-by-default isolation.** Generated code runs in an ephemeral, non-root,
  read-only-rootfs, resource-capped container with the network **severed** before execution.

## Architecture

| Layer | Module | Responsibility |
| --- | --- | --- |
| Orchestration | `graph/` | Cyclical state machine (LangGraph): plan → synthesize → execute → classify → recalculate → finalize/halt |
| Model gateway | `routing/` | LiteLLM wrapper, a two-tier routing matrix (fast vs. reasoning), cache-safe prompt assembly |
| Runtime sandbox | `runtime/` | `ExecutionBackend` protocol, container lifecycle, two-phase install→sever→execute, error classification, dependency resolution |
| Knowledge | `knowledge/` | Optional crowd-intelligence layer: learned import→package hints and error patterns (noop / local SQLite / remote HTTP) |
| Telemetry | `telemetry/` | Rich console logging, run summaries, token/latency metrics |
| Replies | `replies/` | Deterministic private-chat rules plus pluggable Telegram/LINE-style channel adapters |
| Models | `models/` | Shared frozen Pydantic vocabulary (state, results, errors, knowledge) |

```
src/workflow_gps/
├── cli.py            # `wfgps` command-line entry point
├── config.py         # Settings + build_workflow_gps() factory
├── graph/            # builder, nodes, edges
├── routing/          # gateway, matrix, prompting
├── runtime/          # backend, isolation, contract, dependency, sandbox_shim
├── knowledge/        # client, remote, auth, scrubbing, signature
├── telemetry/        # logging, metrics
└── models/           # state, results, errors, knowledge
config/               # models.yaml (local vLLM), openai.yaml, openai-docker.yaml
docker/               # sandbox.Dockerfile, entrypoint.py
tests/                # unit + integration tests
```

## Deterministic Telegram replies

Workflow-GPS can learn repetitive private-chat replies locally and reuse them without
calling a model. Static rules remain available as optional seeds, but the example starts
empty and the primary path is demonstration-based learning.

1. Create a Telegram bot with BotFather and keep its token out of the rules file.
2. For replies on behalf of your account, connect the bot to Telegram Business and grant
   it permission to reply to messages. A normal bot can only reply as itself.
3. Start long polling:

```bash
export TELEGRAM_BOT_TOKEN="..."
wfgps telegram --reply-config config/replies.example.json

# Poll once, useful for a smoke test:
wfgps telegram --reply-config config/replies.example.json --once
```

Only private text messages are considered. When a new Business message has no known
reply, it is remembered but not answered. Reply manually from the Business account within
ten minutes; Workflow-GPS stores that prompt/reply pair in
`~/.workflow-gps/learned-replies.db`. The next exact normalized prompt is answered on
behalf of the same Business connection. Replying directly to the message gives the most
reliable pairing. Bot-generated replies are never learned, which prevents feedback loops.

Use `--reply-memory none` to disable learning or `--reply-memory-db PATH` to choose the
local SQLite file. `ReplyFallback` remains the extension point for a future model or
human-review path; learned matches never invoke it.

Channels that do not expose the account owner's outgoing messages can still be taught
explicitly. This stores the pair in the same local database—no rule-file edit or model
call is involved:

```bash
wfgps reply-teach "Have you arrived?" "I have arrived."
```

This integration uses the official Bot API, so personal-account replies require a bot
connected to a Telegram Business account; it does not automate a normal user session.
The channel-neutral `ChannelAdapter` protocol is the port intended for LINE and other
apps, including a future first-party conversation gateway.

The polling cursor is persisted at `~/.workflow-gps/telegram-offset.json` by default so
confirmed updates are not replayed on an ordinary restart. Override it with
`--offset-file`; each file is scoped to a non-secret fingerprint of the bot token.

## Record and replay an exact CLI skill

The first operational-skill vertical slice records one trusted local command, captures
workspace state and output artifacts, and compiles an exact reusable skill. Recording
never guesses parameters from a single demonstration.

```bash
wfgps skill-record \
  --name "Normalize report" \
  --workspace ./work/example \
  --allow-executable python \
  --approve-write \
  -- python normalize.py input.txt output.txt

wfgps skill-list
wfgps skill-inspect SKILL_ID
wfgps skill-replay SKILL_ID --dry-run

# Delete/reset the demonstrated output first, then run against the same input state:
wfgps skill-run SKILL_ID \
  --workspace ./work/example \
  --allow-executable python \
  --approve-write
```

Replay is blocked when the demonstrated input fingerprint changes, write approval is
absent, an executor capability is missing, the command fails, or expected artifact hashes
do not match. The local CLI adapter uses `shell=False`, but it is not an OS sandbox;
allow-listed commands must still be trusted. Untrusted execution belongs in the Docker or
future restricted-worker composition.

## Unified orchestrator

`workflow_gps.orchestrator` connects the vertical slices into one resumable
runtime (see [docs/adr/0002-unified-run-state.md](docs/adr/0002-unified-run-state.md)).
A workflow flows through:

```text
intake -> guided clarification -> semantic grounding -> route optimization
-> human-control evaluation -> confirmation or approval wait -> execution
-> outcome monitoring -> automatic recovery or incident escalation
-> finalization and route learning
```

The whole workflow is one versioned, serializable `RunState`. It pauses for
clarification, confirmation, approval, or an incident and resumes later — even in
a different process — without losing state, because pause/resume is just saving
and reloading that object. Execution is gated by a hard preflight guard that is
re-derived from the recorded decisions on every attempt (including post-incident
retries), so no path reaches execution without resolved requirements, a
non-excluded route, satisfied human control, and available capabilities.

Driving a workflow is a library API (`WorkflowOrchestrator.start` / `step` /
`resume`); the deterministic default stage adapters are offline and compose the
existing skill core. Natural-language intake and production executors arrive on
later branches. Durable runs are inspectable from the CLI:

```bash
wfgps workflow-list                       # runs and their phase / pause
wfgps workflow-status RUN_ID              # phase, pending pause, and history
wfgps workflow-status RUN_ID --json       # the full serialized run state
```

## Durable runtime

`workflow_gps.durable` makes long-running workflows safe across restarts and
multiple workers. It is built from deployment-neutral ports with a versioned local
SQLite + filesystem adapter today; the same contract is what a PostgreSQL +
object-store deployment implements in production.

- **Durable task queue** — leases, heartbeats, cancellation, retry with backoff,
  dead-lettering, and expired-lease reclaim. Idempotent enqueue.
- **Idempotency ledger** — every externally visible mutation runs at most once, so
  re-driving a task after a crash never duplicates its effects.
- **Transactional outbox** — events/notifications are staged in the *same*
  transaction as the state change and delivered at-least-once by a relay.
- **Hash-linked audit log** — append-only and tamper-evident; reconstructs and
  verifies the complete execution history.
- **Object storage** — content-addressed local blobs for large evidence/artifacts.
- **Backup, restore, retention, deletion** — operational data workflows.

`DurableWorkflowService` ties these to the orchestrator: a run-state checkpoint and
its announcement commit atomically, and a crashed worker's task is reclaimed and
re-driven from the last checkpoint without losing or duplicating work.

## Identity and RBAC

`workflow_gps.identity` makes identity and authority enforceable rather than
simulated. Three rules are structural:

- **Identity comes only from a verified assertion.** An OIDC token is validated
  against a configured provider (issuer, audience, expiry, not-before; `alg: none`
  and algorithm confusion rejected) and turned into an expiring, revocable session.
  A caller cannot self-verify by asserting claims.
- **Authority comes from stored grants, not token text.** Reviewer/approver
  permissions are derived from tenant-scoped role and authority-grant records. A
  token that claims a role grants nothing without a stored grant.
- **Tenants are isolated.** Every store query is tenant-scoped; cross-tenant access
  raises `CrossTenantError`.

Approvals are minted only from an authorized session (`IdentityApprovalAuthority`),
with self-approval, expired grants, confused-deputy scope mismatches, and step-up
(authentication-assurance) all enforced. The token signature verifier is pluggable:
a stdlib HMAC verifier ships for local/test use, and a JWKS-backed asymmetric
verifier is the production adapter.

## Worker control plane

`workflow_gps.worker` separates planning and public APIs from privileged execution.

- **The control plane runs no code and holds no credentials.** It plans and
  dispatches; workers execute. There is no `execute` method and no backend or
  secret on the control plane.
- **Signed, single-use leases authorize execution.** Each task is dispatched with
  an HMAC-signed, expiring, audience-bound lease verified against a
  consumption/revocation ledger, so a lost (forged), duplicated (replayed),
  expired, or revoked lease cannot execute.
- **Isolation is enforced.** Untrusted synthesized code may run only on Docker (or
  a stronger restricted worker); the subprocess backend is reserved for explicitly
  trusted local skills. The worker checks this before running.
- **Outbound-only local agents** serve desktop and private-network resources: they
  poll the control plane (no inbound port) and resolve local credentials
  themselves, so those credentials never reach the control plane.

Workers also report health/capacity, support cancellation (which revokes the
lease), enforce a wall-clock timeout, and are quarantined after repeated failures.

## Provider adapters

`workflow_gps.providers` replaces provider simulations with contract-tested
integrations that all sit behind a credential vault.

- **Google** — authorization-code/OIDC with PKCE: build the consent URL, validate
  the callback, exchange the code, refresh, and revoke; capabilities map to scopes.
- **OpenAI** — API key, plus organization/project service-identity headers.
- **Anthropic** — API key, or the managed enterprise gateway (bearer token).
- **Shared pipeline** — capability discovery, a token-bucket rate limiter, spend
  budgets, request ids, idempotency keys (replays are cached, not re-sent), retries
  with classified errors, and HTTP-status → error classification.
- **Credentials stay in the vault.** Adapters hold only a `CredentialRef` and mint
  an auth header at call time; the secret reaches the provider transport and nothing
  else — not adapter state, audit logs, results, or exceptions.

Every adapter passes one shared contract suite — capability, revocation,
idempotency, and secret-leakage — run through an injected transport, so a real HTTP
transport (the one production seam) drops in without changing the adapters.

## Desktop shell

`workflow_gps.desktop` is the first product surface: a local single-user
application service (`DesktopService`) that a desktop UI binds to over a loopback
boundary. The recommended composition is

```text
desktop UI -> local loopback API -> DesktopService -> unified service
-> SQLite + filesystem -> OS credential vault -> isolated local worker
```

It presents every screen as a frozen, secret-free view-model: task entry and
guided questions, route preview with cost and exclusion explanations,
confirmation/approval/incident inboxes, workflow timeline, cancellation, recovery,
and a verifiable audit view, plus provider connection management, Docker/worker
health with trusted-vs-untrusted labels, offline policy, and local export/deletion.

Two properties are structural: the shell has **no execution path** and routes
approvals only through an authorized identity session, so the UI cannot bypass
backend policy; and no view ever carries a provider secret. The GUI and the
loopback transport are the remaining product layer built on this service.

## HTTP gateway

`workflow_gps.gateway` is a private, tenant-aware HTTP control-plane prototype,
written as a transport-agnostic application over `Request`/`Response` (a WSGI/ASGI
binding is the production seam) on top of the durable runtime:

```text
HTTP API -> OIDC auth + tenant RBAC -> durable transaction/outbox + queue
-> isolated worker -> event stream / status + SSE API
```

- Versioned REST surface (`/v1`) for runs, questions, routes, approvals, incidents,
  provider connections, and feedback, with a served OpenAPI document.
- OIDC bearer auth, tenant-aware RBAC, per-tenant quotas and rate limits, and
  request idempotency (a duplicate submission returns the same run).
- **Asynchronous submission** — `POST /v1/runs` returns `202` with a run id;
  progress is read via status, the SSE event stream, or the audit export, so a long
  run is never a synchronous request.
- Verified, replay-protected webhooks (HMAC + timestamp tolerance + delivery-id
  dedupe), pagination, cancellation, security headers, CORS, and metrics.

Because it runs on the durable runtime, two gateway processes over the same
database share one consistent set of runs, and cross-tenant access is refused.

## Requirements

- Python **3.11+**
- For the engine: `langgraph`, `litellm` (installed via the `engine` extra)
- For the Docker backend: Docker Desktop / Engine + the `docker` Python SDK
- An OpenAI-compatible model endpoint — either a **local** server (vLLM, Ollama, LM Studio)
  or the **hosted OpenAI API**

## Installation

There is one supported install path — an editable install from the repository
root (a single `pyproject.toml` is canonical; there is no longer a second one
under `src/`):

```bash
pip install -e ".[engine]"
```

Optional extras layer on top of the same command:

```bash
pip install -e ".[engine,docker]"   # add Docker backend support
pip install -e ".[engine,dev]"      # add dev tooling (pytest, mypy, ruff)
```

This exposes the `wfgps` command (equivalent to `python -m workflow_gps.cli`).

## Configuration

Settings load from a YAML file and can be layered with environment overrides. Three
templates ship in `config/`:

- `config/models.yaml` — local vLLM defaults (Qwen fast tier, Llama reasoning tier)
- `config/openai.yaml` — hosted OpenAI (`gpt-4o-mini` / `gpt-4o`), subprocess backend
- `config/openai-docker.yaml` — hosted OpenAI + the Docker sandbox backend

Environment overrides (applied on top of any config):

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Credential for the OpenAI-compatible endpoint |
| `WFGPS_CONFIG` | Path to a settings YAML |
| `WFGPS_API_BASE` | Override both tiers' endpoint |
| `WFGPS_FAST_MODEL` / `WFGPS_REASONING_MODEL` | Override per-tier model strings |
| `WFGPS_BACKEND` | `subprocess` or `docker` |
| `WFGPS_PINNED_INDEX_URL` | Package index for Phase-A installs |

> **Note on OpenAI:** the hosted API rejects the `top_k` sampling parameter (a
> local-model knob). The `openai*.yaml` configs set `top_k: null` for this reason.

## Usage

```bash
# Print the effective settings
wfgps show-config --config config/openai.yaml

# Run an intent (human-readable panel)
wfgps run "slugify the title Hello World" --config config/openai.yaml

# Machine-readable JSON result
wfgps run "convert a list of numbers into their squares" --config config/openai.yaml --json

# Run inside the hardened Docker sandbox
wfgps run "use the markdown library to convert '# Hi' to HTML" --config config/openai-docker.yaml --json

# Flip the backend without a separate config
wfgps run "..." --config config/openai.yaml --backend docker
```

### Example (self-healing dependency install)

```text
synthesize: fast tier
recalculate: queued 'markdown' for markdown      # 1st attempt missing the package
{ "success": true, "answer": { "result": "<h1>Hi</h1>" }, "recalc_count": 1, "attempts": 2 }
```

## Execution backends

| Backend | Isolation | When to use |
| --- | --- | --- |
| `subprocess` | **None** — runs on the host, shares kernel/network | Dev / fallback only; never for untrusted intents |
| `docker` | Ephemeral container, non-root, read-only rootfs, resource caps, **network severed before execution** | The real isolation boundary |

### Build the Docker sandbox image

From the repository root (the build context must be the repo root):

```bash
docker build -f docker/sandbox.Dockerfile -t workflow-gps-sandbox:latest .
```

The image is intentionally minimal (Python + `uv` + a non-root user + the in-container
entrypoint). Dependencies, the result shim, and the user script are injected per run.

## Knowledge layer (optional)

A crowd-intelligence layer learns `import → package` mappings (so `cv2` resolves to
`opencv-python`, etc.) and error patterns, improving resolution over time:

```bash
wfgps run "..." --knowledge local                       # local SQLite cache
wfgps run "..." --knowledge remote                       # needs WFGPS_KNOWLEDGE_URL + _TOKEN
```

Stored data is scrubbed of secrets/PII before it is ever persisted or uploaded.

## Adapter maturity

Each swappable seam ships several implementations. Which ones are
production-capable today versus experimental or test-only is documented in
[docs/ADAPTER_MATURITY.md](docs/ADAPTER_MATURITY.md). In short: the Docker
backend, the LiteLLM gateway, and the local SQLite stores are the
production-capable local-alpha path; the subprocess backend, remote knowledge
client, Telegram channel, and CLI action executor are experimental; in-memory
and remote-mock stores are test-only.

## Testing

```bash
pip install -e ".[engine,dev]"
pytest
```

Tests run fully offline using injected fakes (`StubBackend`, `FakeGateway`), so no live
model endpoint or Docker daemon is required. Tests that need optional capabilities
(`uv`, `langgraph`, Docker) are auto-skipped when those aren't available.

## License

Not yet specified.
