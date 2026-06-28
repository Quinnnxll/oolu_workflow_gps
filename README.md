# Workflow-GPS

A real-time **navigation engine for local LLM agentic workflows**.

Workflow-GPS treats APIs, local files, and system tasks as a *road network*, a user's
intent as a *destination*, and the agent loop as a *real-time navigation engine* (think
Google Maps). When a step fails — a missing package, a runtime exception, environmental
drift — the engine doesn't give up: it captures the error, **"recalculates,"** alters its
route (e.g. auto-installs a dependency, re-synthesizes the code), and keeps driving toward
the goal.

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

## Requirements

- Python **3.11+**
- For the engine: `langgraph`, `litellm` (installed via the `engine` extra)
- For the Docker backend: Docker Desktop / Engine + the `docker` Python SDK
- An OpenAI-compatible model endpoint — either a **local** server (vLLM, Ollama, LM Studio)
  or the **hosted OpenAI API**

## Installation

From the repository root (editable install recommended for development):

```bash
pip install -e "src[engine]"
# add the Docker backend support:
pip install -e "src[engine,docker]"
# add dev tooling (pytest, mypy, ruff):
pip install -e "src[engine,dev]"
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

## Testing

```bash
pip install -e "src[engine,dev]"
pytest
```

Tests run fully offline using injected fakes (`StubBackend`, `FakeGateway`), so no live
model endpoint or Docker daemon is required. Tests that need optional capabilities
(`uv`, `langgraph`, Docker) are auto-skipped when those aren't available.

## License

Not yet specified.
