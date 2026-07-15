"""Command-line entry point for OoLu.

    oolu run "convert sales.csv into a bar chart"
    oolu run "slugify a title" --backend docker --knowledge local
    oolu run "..." --json                 # machine-readable result
    oolu record "book a flight" --url https://air.example  # learn a browser skill
    oolu show-config                       # print effective settings
    oolu telegram --reply-config replies.json
    oolu version

Stdlib argparse only. The engine builder is injectable (``builder=``) so the CLI is
testable without a live vLLM/litellm stack; in production it defaults to the real
``build_oolu`` which talks to the configured OpenAI-compatible endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid

try:
    from . import __version__
    from .telemetry import configure_logging, get_logger, render_result
except ImportError:
    # Running the file directly (`python src/oolu/cli.py`) strips the
    # package context and the relative imports above fail with a traceback
    # that blames the wrong thing. Fail with directions instead.
    if __package__:
        raise
    sys.stderr.write(
        "error: cli.py is part of the oolu package and cannot run as "
        "a bare file.\n"
        "\n"
        "  Easiest (no development tools needed): run setup.bat (Windows) or\n"
        "  ./setup.sh (macOS/Linux) from the repository folder - it sets\n"
        "  everything up and opens the app in your browser.\n"
        "\n"
        '  Developers:  pip install -e ".[serve]"   then   oolu --help\n'
        "               (or: python -m oolu.cli --help)\n"
    )
    sys.exit(2)

_DEFAULT_KNOWLEDGE_DB = os.path.expanduser("~/.oolu/knowledge.db")
_DEFAULT_SCRIPT_CACHE_DB = os.path.expanduser("~/.oolu/script-cache.db")
_DEFAULT_TELEGRAM_OFFSET = os.path.expanduser("~/.oolu/telegram-offset.json")
_DEFAULT_REPLY_MEMORY_DB = os.path.expanduser("~/.oolu/learned-replies.db")
_DEFAULT_SKILL_DB = os.path.expanduser("~/.oolu/skills.db")
_DEFAULT_WORKFLOW_DB = os.path.expanduser("~/.oolu/workflows.db")


class _CliError(Exception):
    """User-facing configuration/usage error (exit code 2)."""


# --------------------------------------------------------------------------- #
# Argument parser.                                                            #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oolu", description="OoLu — self-healing local agent engine."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run an intent through the engine")
    run.add_argument("intent", help="the task to accomplish, in natural language")
    run.add_argument(
        "--config", metavar="PATH", help="path to a models.yaml settings file"
    )
    run.add_argument(
        "--backend",
        choices=["subprocess", "docker"],
        help="override the execution backend",
    )
    run.add_argument(
        "--knowledge",
        choices=["none", "local", "remote"],
        default="none",
        help="knowledge layer (default: none)",
    )
    run.add_argument(
        "--knowledge-db",
        metavar="PATH",
        default=_DEFAULT_KNOWLEDGE_DB,
        help="SQLite path for local/remote cache",
    )
    run.add_argument(
        "--script-cache",
        choices=["none", "local"],
        default="none",
        help="synthesized-script cache (default: none)",
    )
    run.add_argument(
        "--script-cache-db",
        metavar="PATH",
        default=_DEFAULT_SCRIPT_CACHE_DB,
        help="SQLite path for the local script cache",
    )
    run.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="console log level",
    )
    run.add_argument(
        "--json", action="store_true", help="emit the result as JSON instead of a panel"
    )
    run.add_argument(
        "--no-preflight",
        action="store_true",
        help="skip the engine/model-server checks that run before the intent",
    )

    telegram = sub.add_parser(
        "telegram", help="run deterministic replies for a Telegram bot"
    )
    telegram.add_argument(
        "--reply-config",
        required=True,
        metavar="PATH",
        help="local JSON file containing context and reply rules",
    )
    telegram.add_argument(
        "--token-env",
        default="TELEGRAM_BOT_TOKEN",
        metavar="NAME",
        help="environment variable containing the bot token",
    )
    telegram.add_argument("--poll-timeout", type=int, default=25, metavar="SECONDS")
    telegram.add_argument(
        "--offset-file",
        default=_DEFAULT_TELEGRAM_OFFSET,
        metavar="PATH",
        help="persistent update cursor (prevents replay after restart)",
    )
    telegram.add_argument(
        "--reply-memory",
        choices=["none", "local"],
        default="local",
        help="learn prompt/reply pairs from manual Business replies (default: local)",
    )
    telegram.add_argument(
        "--reply-memory-db",
        default=_DEFAULT_REPLY_MEMORY_DB,
        metavar="PATH",
        help="SQLite path for learned replies",
    )
    telegram.add_argument("--once", action="store_true", help="poll once, then exit")
    telegram.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    teach = sub.add_parser("reply-teach", help="teach one local prompt/reply pair")
    teach.add_argument("prompt", help="incoming message to recognize")
    teach.add_argument("reply", help="reply to reuse")
    teach.add_argument(
        "--scope",
        default="telegram-bot",
        help="account/channel scope (default: telegram-bot)",
    )
    teach.add_argument(
        "--reply-memory-db",
        default=_DEFAULT_REPLY_MEMORY_DB,
        metavar="PATH",
        help="SQLite path for learned replies",
    )

    skill_list = sub.add_parser(
        "skill-list", help="list locally stored reusable skills"
    )
    skill_list.add_argument("--skill-db", default=_DEFAULT_SKILL_DB, metavar="PATH")
    skill_list.add_argument("--json", action="store_true")

    skill_inspect = sub.add_parser("skill-inspect", help="inspect one reusable skill")
    skill_inspect.add_argument("skill_id")
    skill_inspect.add_argument("--skill-db", default=_DEFAULT_SKILL_DB, metavar="PATH")

    skill_replay = sub.add_parser(
        "skill-replay", help="inspect a replay plan without executing actions"
    )
    skill_replay.add_argument("skill_id")
    skill_replay.add_argument("--skill-db", default=_DEFAULT_SKILL_DB, metavar="PATH")
    skill_replay.add_argument(
        "--dry-run",
        action="store_true",
        help="required safety flag; no action executor is invoked",
    )

    skill_record = sub.add_parser(
        "skill-record",
        help="record one approved CLI command as an exact reusable skill",
    )
    skill_record.add_argument("--name", required=True)
    skill_record.add_argument("--description", default="Recorded CLI demonstration")
    skill_record.add_argument("--workspace", required=True, metavar="PATH")
    skill_record.add_argument(
        "--allow-executable",
        action="append",
        required=True,
        metavar="NAME_OR_PATH",
        help="repeat for every executable that may run",
    )
    skill_record.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS")
    skill_record.add_argument("--skill-db", default=_DEFAULT_SKILL_DB, metavar="PATH")
    skill_record.add_argument(
        "--approve-write",
        action="store_true",
        help="required acknowledgement that the demonstration may modify the workspace",
    )
    skill_record.add_argument("argv", nargs=argparse.REMAINDER)

    skill_run = sub.add_parser("skill-run", help="execute a validated exact CLI skill")
    skill_run.add_argument("skill_id")
    skill_run.add_argument("--workspace", required=True, metavar="PATH")
    skill_run.add_argument(
        "--allow-executable", action="append", required=True, metavar="NAME_OR_PATH"
    )
    skill_run.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS")
    skill_run.add_argument("--skill-db", default=_DEFAULT_SKILL_DB, metavar="PATH")
    skill_run.add_argument("--idempotency-key", default=None)
    skill_run.add_argument(
        "--approve-write",
        action="store_true",
        help="approve this skill's workspace writes",
    )

    workflow_list = sub.add_parser(
        "workflow-list", help="list orchestrator runs and their phase/pause"
    )
    workflow_list.add_argument(
        "--workflow-db", default=_DEFAULT_WORKFLOW_DB, metavar="PATH"
    )
    workflow_list.add_argument("--json", action="store_true")

    workflow_status = sub.add_parser(
        "workflow-status", help="show one orchestrator run's state and history"
    )
    workflow_status.add_argument("run_id")
    workflow_status.add_argument(
        "--workflow-db", default=_DEFAULT_WORKFLOW_DB, metavar="PATH"
    )
    workflow_status.add_argument("--json", action="store_true")

    skill_register = sub.add_parser(
        "skill-register",
        help="load a skill pack (or the starter pack) into the registry",
    )
    skill_register.add_argument(
        "pack", nargs="?", metavar="PACK", help="path to a skill-pack YAML file"
    )
    skill_register.add_argument(
        "--starter", action="store_true", help="load the built-in starter pack"
    )
    skill_register.add_argument("--config", metavar="PATH", help="models.yaml path")
    skill_register.add_argument(
        "--registry", metavar="PATH", help="registry SQLite path"
    )
    skill_register.add_argument("--json", action="store_true")

    serve = sub.add_parser("serve", help="serve /v1/skills over HTTP (loopback)")
    serve.add_argument("--config", metavar="PATH", help="path to a models.yaml file")
    serve.add_argument("--registry", metavar="PATH", help="skill registry SQLite path")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8756)
    serve.add_argument("--workspace", metavar="PATH", help="CLI executor workspace")
    serve.add_argument(
        "--allow-executable",
        action="append",
        default=[],
        metavar="NAME_OR_PATH",
        help="allow-list a CLI executable for /v1/skills/execute",
    )
    serve.add_argument(
        "--seed-starter",
        action="store_true",
        help="load the built-in starter pack if the registry is empty",
    )
    serve.add_argument(
        "--browser",
        action="store_true",
        help="enable the Playwright browser executor for /v1/skills/execute",
    )
    serve.add_argument(
        "--discover-tools",
        action="store_true",
        help="probe PATH for known CLI tools; expose them at /v1/tools and allow-list them",
    )
    serve.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST",
        help="allow-list a hostname the browser executor may reach",
    )

    record = sub.add_parser(
        "record", help="record a browser demonstration into a learned skill"
    )
    record.add_argument("intent", help="what the demonstration accomplishes")
    record.add_argument("--url", required=True, help="page to start the demo on")
    record.add_argument("--name", help="skill name (default: the intent)")
    record.add_argument("--config", metavar="PATH", help="path to a models.yaml file")
    record.add_argument("--registry", metavar="PATH", help="skill registry SQLite path")
    record.add_argument(
        "--audit-db",
        metavar="PATH",
        help="durable DB whose audit log is correlated as backend system logs",
    )
    record.add_argument(
        "--headless", action="store_true", help="run the browser without a window"
    )
    record.add_argument("--json", action="store_true")

    desktop = sub.add_parser(
        "desktop", help="serve the ADR-0004 loopback transport for the desktop UI"
    )
    desktop.add_argument("--config", metavar="PATH", help="path to a models.yaml file")
    desktop.add_argument("--db", metavar="PATH", help="durable workflow SQLite path")
    desktop.add_argument(
        "--registry", metavar="PATH", help="skill registry SQLite path"
    )
    desktop.add_argument("--host", default="127.0.0.1")
    desktop.add_argument("--port", type=int, default=8765)
    desktop.add_argument(
        "--seed-starter",
        action="store_true",
        help="load the built-in starter pack if the registry is empty",
    )
    desktop.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="open the shell in the default browser once serving",
    )
    host = sub.add_parser(
        "host",
        help="multi-user web hosting: the full gateway with local accounts",
    )
    host.add_argument("--config", metavar="PATH", help="path to a models.yaml file")
    host.add_argument(
        "--data",
        metavar="DIR",
        default=".oolu/host",
        help="data directory (all state lives here; default: .oolu/host)",
    )
    host.add_argument("--host", default="0.0.0.0")
    host.add_argument("--port", type=int, default=8788)
    host.add_argument("--tenant", default="main", help="tenant for the bootstrap admin")
    host.add_argument(
        "--admin", default="admin", metavar="USERNAME", help="bootstrap admin username"
    )
    host.add_argument(
        "--secret-env",
        default="OOLU_HOST_SECRET",
        metavar="NAME",
        help="environment variable holding the token-signing secret "
        "(generated if unset — tokens then die with the process)",
    )
    host.add_argument(
        "--admin-password-env",
        default="OOLU_ADMIN_PASSWORD",
        metavar="NAME",
        help="environment variable holding the first admin's password "
        "(generated and printed once if unset)",
    )
    host.add_argument(
        "--database-url",
        metavar="DSN",
        default=os.environ.get("OOLU_DATABASE_URL") or os.environ.get("DATABASE_URL"),
        help="PostgreSQL DSN for the durable workflow store, so several app "
        "clients share one online database; defaults to a local SQLite file "
        "under --data. Reads OOLU_DATABASE_URL / DATABASE_URL if unset "
        "(needs the 'postgres' extra)",
    )
    host.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="CORS origin permitted to call this host (repeatable) — e.g. the "
        "desktop app's origin. Use '*' to allow any origin",
    )
    host.add_argument(
        "--open-registration",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="allow self-serve e-mail registration (POST /v1/auth/register). "
        "ON by default — a server exists to take accounts; pass "
        "--no-open-registration for a closed install",
    )
    host.add_argument(
        "--global-service",
        action="store_true",
        help="run as the OoLu GLOBAL service: Supernodes here serve the whole "
        "ecosystem with a higher trust score, so the KYC policy (and its "
        "paying-plan gate) is enforced. Edge installs — this device or a "
        "private-network server — leave it off: no KYC, no subscription",
    )
    host.add_argument(
        "--transactions",
        action="store_true",
        help="open the launch guard's transaction port so real cards can be "
        "charged (per-class price settlement and verification still gate). "
        "Requires OOLU_STRIPE_KEY — the port never opens onto test doubles",
    )
    host.add_argument(
        "--ordering",
        action="store_true",
        help="turn on autonomous order placement: the operator master switch "
        "above the per-order consent + 2FA gate. Off by default — the "
        "order-placing hands can browse, but the money step of any order "
        "stays BLOCKED until this is set. Spends the user's own money at a "
        "retailer through their released authorization",
    )

    sub.add_parser("show-config", help="print the effective settings").add_argument(
        "--config", metavar="PATH", help="path to a models.yaml settings file"
    )
    sub.add_parser(
        "doctor", help="check this installation and say exactly what to fix"
    ).add_argument("--config", metavar="PATH", help="path to a models.yaml file")
    backup = sub.add_parser(
        "backup",
        help="copy everything a restore needs into one timestamped folder",
    )
    backup.add_argument(
        "--data",
        metavar="DIR",
        required=True,
        help="the data directory to back up (.oolu/host for a host; "
        "~/.oolu for the desktop)",
    )
    backup.add_argument(
        "--out",
        metavar="DIR",
        default="backups",
        help="where backup folders are created (default: ./backups)",
    )
    # The representative, locally: your voice trains on YOUR machine —
    # messages and adapters never leave it. One-shot by default (train
    # what's due, report, exit); --watch keeps the sweep going.
    rep_status = sub.add_parser(
        "representative-status",
        help="who has a representative here, and whether a refresh is due",
    )
    rep_status.add_argument(
        "--data",
        default=".oolu/unified",
        metavar="DIR",
        help="the install's data directory (desktop default: .oolu/unified;"
        " a host uses .oolu/host)",
    )

    rep_train = sub.add_parser(
        "representative-train",
        help="train due representatives on this machine (nothing leaves it)",
    )
    rep_train.add_argument(
        "--data",
        default=".oolu/unified",
        metavar="DIR",
        help="the install's data directory (desktop default: .oolu/unified;"
        " a host uses .oolu/host)",
    )
    rep_train.add_argument(
        "--base-model",
        default="Qwen/Qwen3-4B-Instruct",
        help="the shared base the LoRA trains on (downloads from the Hub"
        " on first use)",
    )
    rep_train.add_argument(
        "--dpo",
        action="store_true",
        help="stack the preference pass where enough edit pairs exist",
    )
    rep_train.add_argument(
        "--vllm",
        metavar="API_BASE",
        help="local vLLM (/v1 included) to hot-load finished adapters into",
    )
    rep_train.add_argument(
        "--watch",
        action="store_true",
        help="keep sweeping instead of one pass",
    )
    rep_train.add_argument("--poll", type=float, default=30.0, metavar="SECONDS")
    rep_train.add_argument(
        "--floor",
        type=int,
        default=None,
        metavar="N",
        help="override the cold-start floor (exchanges needed to train)",
    )
    rep_train.add_argument(
        "--trainer-command",
        metavar="CMD",
        help="advanced: a custom training command honoring the config/output"
        " contract; {config} is replaced with the config path",
    )

    sub.add_parser("version", help="print the version")
    return parser


# --------------------------------------------------------------------------- #
# Settings / knowledge wiring.                                                #
# --------------------------------------------------------------------------- #
def _cmd_backup(args, out) -> int:
    """One folder that restores the whole install.

    Databases go through SQLite's ONLINE backup API — safe against a
    live server mid-write, unlike a file copy. The keyring's machine.key
    rides along: encrypted model keys are unreadable without it, so a
    backup that skipped it would restore a host with amnesia. When the
    durable store is PostgreSQL the runbook's pg_dump covers it — this
    command still captures the local auxiliary databases.
    """
    import shutil
    import sqlite3
    from datetime import UTC, datetime
    from pathlib import Path

    data = Path(args.data).expanduser()
    if not data.is_dir():
        raise _CliError(f"no data directory at {data}")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    destination = Path(args.out).expanduser() / f"oolu-backup-{stamp}"
    destination.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for name in (
        "host.db",
        "identity.db",
        "users.db",
        "prices.db",
        "traces.db",
        "scripts.db",
    ):
        source_path = data / name
        if not source_path.is_file():
            continue
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(destination / name)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        copied.append(name)
    key_path = data / "machine.key"
    if key_path.is_file():
        shutil.copy2(key_path, destination / "machine.key")
        copied.append("machine.key")
    if not copied:
        raise _CliError(
            f"nothing to back up under {data} — is this really an OoLu"
            " data directory?"
        )
    for name in copied:
        out.write(f"[ok] {name}\n")
    database_url = os.environ.get("OOLU_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if database_url:
        out.write(
            "[--] the durable store is PostgreSQL — back it up with"
            " pg_dump (see docs/operations.md); the files above are the"
            " local auxiliaries\n"
        )
    out.write(f"backup written to {destination}\n")
    return 0


def _load_settings(args):
    # Settings.load() resolves --config (or $OOLU_CONFIG, else defaults) AND layers
    # the OOLU_* env overrides on top. Calling Settings() directly would skip those
    # overrides, so always go through load() even when no config path is given.
    from .config import Settings

    settings = Settings.load(getattr(args, "config", None))
    if getattr(args, "backend", None):
        settings = settings.model_copy(
            update={
                "backend": settings.backend.model_copy(update={"kind": args.backend})
            }
        )
    return settings


def _build_knowledge(kind: str, db_path: str):
    if kind == "none":
        return None
    if db_path and db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    if kind == "local":
        from .knowledge import LocalKnowledgeClient

        return LocalKnowledgeClient(db_path)
    if kind == "remote":
        url = os.environ.get("OOLU_KNOWLEDGE_URL")
        token = os.environ.get("OOLU_KNOWLEDGE_TOKEN")
        if not (url and token):
            raise _CliError(
                "--knowledge remote needs OOLU_KNOWLEDGE_URL and OOLU_KNOWLEDGE_TOKEN"
            )
        from .knowledge import RemoteConfig, RemoteKnowledgeClient, StaticTokenProvider

        return RemoteKnowledgeClient(
            RemoteConfig(base_url=url),
            StaticTokenProvider(token),
            local_db_path=db_path,
        )
    raise _CliError(f"unknown knowledge kind: {kind}")


def _build_script_cache(kind: str, db_path: str):
    if kind == "none":
        return None
    if kind == "local":
        from .cache import LocalScriptCache

        return LocalScriptCache(db_path)
    raise _CliError(f"unknown script cache kind: {kind}")


# --------------------------------------------------------------------------- #
# Defensive validation: fail with directions, never with a socket traceback.  #
# --------------------------------------------------------------------------- #
def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def _probe_endpoint(api_base: str, timeout_s: float = 3.0) -> str | None:
    """None when an HTTP server answers at ``api_base`` — ANY status counts
    (401/404 still prove something is listening); an error string otherwise."""
    import urllib.error
    import urllib.request

    url = api_base.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s):
            return None
    except urllib.error.HTTPError:
        return None  # the server answered; auth/path problems are not "down"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return str(getattr(exc, "reason", exc))


def _needs_openai_key(tier) -> bool:
    return tier.model.startswith("openai/") and "api_key" not in tier.extra_params


def _preflight_run(settings) -> None:
    """The three traps every fresh install falls into, caught up front:
    missing engine extras, no model server listening, no API key. Each
    failure names its one-line fix; ``--no-preflight`` bypasses all three."""
    missing = [m for m in ("langgraph", "litellm") if not _module_available(m)]
    if missing:
        raise _CliError(
            "`oolu run` needs the model engine, which is not installed "
            f"(missing: {', '.join(missing)}).\n"
            '  fix : pip install "oolu[engine]"\n'
            "  then: oolu doctor   (checks the rest of your setup)"
        )
    fast = settings.routing.fast
    if fast.api_base:
        error = _probe_endpoint(fast.api_base)
        if error is not None:
            raise _CliError(
                f"no model server is answering at {fast.api_base} ({error}).\n"
                "  OoLu defaults to a LOCAL OpenAI-compatible server "
                "(vLLM / Ollama / LM Studio)\n"
                f"  serving {fast.model}.\n"
                "  fix : start one there, or point at your own endpoint with "
                "--config models.yaml\n"
                "  more: oolu doctor   (skip this check with --no-preflight)"
            )
    if _needs_openai_key(fast) and not os.environ.get("OPENAI_API_KEY"):
        raise _CliError(
            "OPENAI_API_KEY is not set. litellm requires it even for local "
            "servers — any value\n"
            "  works for vLLM (e.g. set OPENAI_API_KEY=EMPTY). Skip this "
            "check with --no-preflight."
        )


def _cmd_doctor(args, out) -> int:
    """Every silent installation trap, made loud — with its fix."""
    settings = _load_settings(args)
    failures = 0

    def check(ok: bool, label: str, detail: str, hint: str | None = None) -> None:
        nonlocal failures
        out.write(f"  [{'ok' if ok else 'XX'}] {label}: {detail}\n")
        if not ok:
            failures += 1
            if hint:
                out.write(f"       fix: {hint}\n")

    def note(label: str, detail: str, hint: str) -> None:
        out.write(f"  [--] {label}: {detail}\n")
        out.write(f"       {hint}\n")

    out.write("OoLu doctor\n")
    check(
        sys.version_info >= (3, 11),
        "python",
        sys.version.split()[0],
        "install Python 3.11+ from https://www.python.org/downloads/",
    )

    data_dir = os.path.expanduser("~/.oolu")
    try:
        os.makedirs(data_dir, exist_ok=True)
        probe_path = os.path.join(data_dir, ".doctor-probe")
        with open(probe_path, "w", encoding="utf-8") as probe:
            probe.write("ok")
        os.remove(probe_path)
        check(True, "data dir", f"{data_dir} is writable")
    except OSError as exc:
        check(
            False,
            "data dir",
            f"{data_dir}: {exc}",
            "fix the directory's permissions (app data lives there)",
        )

    if _module_available("uvicorn"):
        check(True, "desktop shell", "uvicorn installed")
    else:
        note(
            "desktop shell",
            "uvicorn not installed",
            "needed for `oolu desktop` / `oolu serve`: "
            'pip install "oolu[serve]"',
        )

    engine_missing = [m for m in ("langgraph", "litellm") if not _module_available(m)]
    if engine_missing:
        note(
            "model engine",
            "not installed: " + ", ".join(engine_missing),
            'needed only for `oolu run`: pip install "oolu[engine]"',
        )
    else:
        check(True, "model engine", "langgraph + litellm installed")
        for name in ("fast", "reasoning"):
            tier = getattr(settings.routing, name)
            if not tier.api_base:
                out.write(
                    f"  [--] {name} tier: {tier.model} via the provider default\n"
                )
                continue
            error = _probe_endpoint(tier.api_base)
            check(
                error is None,
                f"{name} tier",
                f"{tier.model} @ {tier.api_base}"
                + ("" if error is None else f" — {error}"),
                "start a local OpenAI-compatible model server (vLLM / Ollama / "
                "LM Studio) there, or point at your own endpoint with "
                "--config models.yaml",
            )
        if _needs_openai_key(settings.routing.fast):
            check(
                bool(os.environ.get("OPENAI_API_KEY")),
                "api key",
                "OPENAI_API_KEY "
                + ("is set" if os.environ.get("OPENAI_API_KEY") else "is not set"),
                "litellm requires it even for local servers; any value works "
                "for vLLM (e.g. OPENAI_API_KEY=EMPTY)",
            )

    for module, extra, what in (
        ("playwright", "browser", "browser skills"),
        ("docker", "docker", "the docker backend"),
    ):
        if not _module_available(module):
            note(
                what,
                f"{module} not installed",
                f'optional: pip install "oolu[{extra}]"',
            )

    out.write("\n")
    if failures:
        out.write(f"{failures} problem(s) found — fixes above.\n")
        return 1
    out.write("Everything this machine needs is in place.\n")
    return 0


# --------------------------------------------------------------------------- #
# Commands.                                                                    #
# --------------------------------------------------------------------------- #
def _result_to_dict(result) -> dict:
    tier = getattr(result, "final_tier", None)
    return {
        "success": getattr(result, "success", False),
        "status": getattr(getattr(result, "status", None), "value", None),
        "answer": getattr(result, "answer", None),
        "failure_reason": getattr(result, "failure_reason", None),
        "recalc_count": getattr(result, "recalc_count", 0),
        "tier_escalations": getattr(result, "tier_escalations", 0),
        "final_tier": getattr(tier, "value", None),
        "attempts": getattr(result, "attempts", 0),
        "cache_key": getattr(result, "cache_key", None),
        "cache_hit": getattr(result, "cache_hit", False),
        "cache_kind": getattr(result, "cache_kind", None),
        "cache_status": getattr(result, "cache_status", None),
    }


def _cmd_run(args, builder, out, *, preflight: bool = False) -> int:
    configure_logging(level=args.log_level)
    log = get_logger("cli")
    settings = _load_settings(args)
    if preflight:
        # Catch the classic fresh-install traps (missing engine extra, no
        # model server, no API key) with directions, before any engine
        # machinery produces a misleading traceback.
        _preflight_run(settings)
    knowledge = _build_knowledge(args.knowledge, args.knowledge_db)
    script_cache = _build_script_cache(args.script_cache, args.script_cache_db)
    log.info("navigating: %s", args.intent)

    builder_kwargs = {"knowledge": knowledge}
    if script_cache is not None:
        builder_kwargs["script_cache"] = script_cache
    try:
        engine = builder(settings, **builder_kwargs)
        result = engine.run(args.intent)
    except ModuleNotFoundError as exc:
        if exc.name in ("langgraph", "litellm"):
            raise _CliError(
                f'missing dependency {exc.name!r} — pip install "oolu[engine]"'
            ) from exc
        raise
    finally:
        if knowledge is not None and hasattr(knowledge, "close"):
            knowledge.close()  # flushes best-effort remote uploads
        if script_cache is not None:
            script_cache.close()

    if args.json:
        out.write(json.dumps(_result_to_dict(result), indent=2) + "\n")
    else:
        render_result(result)
    return 0 if getattr(result, "success", False) else 1


def _cmd_show_config(args, out) -> int:
    s = _load_settings(args)
    out.write("OoLu effective settings:\n")
    out.write(
        f"  fast tier      : {s.routing.fast.model} @ {s.routing.fast.api_base}\n"
    )
    out.write(
        f"  reasoning tier : {s.routing.reasoning.model} @ {s.routing.reasoning.api_base}\n"
    )
    out.write(f"  backend        : {s.backend.kind}\n")
    out.write(f"  max recalcs    : {s.edges.max_recalcs}\n")
    out.write(
        f"  limits         : mem={s.limits.memory_mb}MB wall={s.limits.wall_timeout_s}s "
        f"install={s.limits.install_timeout_s}s read_only_rootfs={s.limits.read_only_rootfs}\n"
    )
    return 0


def _cmd_telegram(args) -> int:
    configure_logging(level=args.log_level)
    log = get_logger("telegram")
    token = os.environ.get(args.token_env)
    if not token:
        raise _CliError(f"{args.token_env} is not set")

    learned = None
    try:
        from .replies import (
            DeterministicReplyEngine,
            FileOffsetStore,
            LocalLearnedReplyStore,
            ReplyBot,
            ReplyConfig,
        )
        from .replies.channels import ChannelError, TelegramAdapter

        config = ReplyConfig.load(args.reply_config)
        if args.reply_memory == "local":
            learned = LocalLearnedReplyStore(args.reply_memory_db)
        bot = ReplyBot(
            TelegramAdapter.from_token(token),
            DeterministicReplyEngine(config.rules),
            config.context,
            learned=learned,
        )
        token_identity = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        offsets = FileOffsetStore(
            args.offset_file, identity=f"telegram:{token_identity}"
        )
        offset = offsets.load()
        while True:
            stats, offset = bot.run_once(offset=offset, timeout_s=args.poll_timeout)
            offsets.save(offset)
            if stats.received or args.once:
                log.info(
                    "received=%d sent=%d learned=%d taught=%d rule=%d fallback=%d",
                    stats.received,
                    stats.sent,
                    stats.learned_replies,
                    stats.learned_pairs,
                    stats.rule_replies,
                    stats.fallback_replies,
                )
            if args.once:
                return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, ChannelError) as exc:
        raise _CliError(str(exc)) from exc
    finally:
        if learned is not None:
            learned.close()


def _cmd_reply_teach(args, out) -> int:
    from .replies import LocalLearnedReplyStore

    store = LocalLearnedReplyStore(args.reply_memory_db)
    try:
        store.teach(scope=args.scope, prompt=args.prompt, reply=args.reply)
    finally:
        store.close()
    out.write(f"learned reply in scope '{args.scope}'\n")
    return 0


def _representative_store(data: str):
    from pathlib import Path

    from .representative import RepresentativeStore

    path = Path(data).expanduser() / "representative.db"
    if not path.exists():
        raise _CliError(
            f"no representative data at {path} — is --data pointing at the"
            " install's directory, and has anyone turned the mode on?"
        )
    return RepresentativeStore(path)


def _cmd_representative_status(args, out) -> int:
    from .representative import COLD_START_FLOOR
    from .representative.trainer import refresh_reason

    store = _representative_store(args.data)
    try:
        scopes = store.scopes()
        if not scopes:
            out.write("no representatives are turned on here\n")
            return 0
        for scope in scopes:
            active = store.active_adapter(scope)
            count = store.exchange_count(scope)
            voice = (
                f"v{active['version']} (ppl {active['holdout_ppl']})"
                if active is not None
                else "base — no adapter yet"
            )
            due = refresh_reason(store, scope)
            if due:
                tail = f"due: {due}"
            elif active is None and count < COLD_START_FLOOR:
                tail = f"gathering voice ({count}/{COLD_START_FLOOR} exchanges)"
            else:
                tail = "up to date"
            out.write(
                f"{scope}: mode={store.mode(scope)}"
                f" exchanges={count} voice={voice} — {tail}\n"
            )
    finally:
        store.close()
    return 0


def _cmd_representative_train(args, out) -> int:
    """Local training, whole and offline: sweep what's due onto the
    dedicated queue and drain it — dataset, QLoRA from base, artifact,
    registry, (optionally) a live vLLM load. The same worker a GPU host
    runs; here it simply runs where the messages already live."""
    import shlex
    from pathlib import Path

    from .durable.artifacts import FilesystemArtifactStore
    from .durable.connection import DurableConnection
    from .durable.queue import DurableTaskQueue
    from .representative import COLD_START_FLOOR, VllmAdapterServer
    from .representative.trainer import (
        SubprocessPreferenceTrainer,
        SubprocessTrainer,
        TrainerWorker,
        sweep,
    )

    store = _representative_store(args.data)
    data = Path(args.data).expanduser()
    queue = DurableTaskQueue(DurableConnection(data / "representative-queue.db"))
    trainer = (
        SubprocessTrainer(shlex.split(args.trainer_command))
        if args.trainer_command
        else SubprocessTrainer()
    )
    floor = args.floor if args.floor is not None else COLD_START_FLOOR
    worker = TrainerWorker(
        store,
        queue,
        trainer,
        FilesystemArtifactStore(data / "adapters" / "artifacts"),
        base_model=args.base_model,
        work_root=data / "adapters" / "work",
        adapters_root=data / "adapters" / "live",
        serving=(
            VllmAdapterServer(store, api_base=args.vllm) if args.vllm else None
        ),
        preference_trainer=SubprocessPreferenceTrainer() if args.dpo else None,
        floor=floor,
    )
    try:
        if args.watch:
            out.write("watching for due representatives — Ctrl+C stops\n")
            worker.run_forever(poll_s=args.poll)
            return 0  # pragma: no cover - run_forever never returns
        due = sweep(store, queue, floor=floor)
        out.write(f"{len(due)} scope(s) due\n")
        failures = 0
        while (result := worker.run_once()) is not None:
            scope = result.get("scope", "?")
            if "error" in result:
                failures += 1
                out.write(f"{scope}: FAILED — {result['error']}\n")
            elif result.get("skipped"):
                out.write(
                    f"{scope}: skipped — {result['examples']} usable exchanges,"
                    f" the floor is {result['floor']}\n"
                )
            else:
                out.write(
                    f"{scope}: v{result['version']} trained"
                    f" ({result['examples']} examples"
                    f", ppl {result['holdout_ppl']}"
                    f", dpo pairs {result['dpo_pairs']}) — "
                    + (
                        "ACTIVE — this voice now drafts"
                        if result["activated"]
                        else "shelved (worse than the live voice)"
                    )
                    + "\n"
                )
        return 1 if failures else 0
    finally:
        store.close()


def _cmd_skill_list(args, out) -> int:
    from .skills import LocalSkillStore

    store = LocalSkillStore(args.skill_db)
    try:
        skills = store.list()
    finally:
        store.close()
    if args.json:
        out.write(
            json.dumps(
                [
                    {
                        "id": skill.id,
                        "name": skill.name,
                        "application": skill.signature.application,
                        "adapter": skill.signature.adapter,
                        "updated_at": skill.updated_at.isoformat(),
                    }
                    for skill in skills
                ],
                indent=2,
            )
            + "\n"
        )
    elif not skills:
        out.write("No stored skills.\n")
    else:
        for skill in skills:
            out.write(
                f"{skill.id}  {skill.name}  "
                f"[{skill.signature.application}/{skill.signature.adapter}]\n"
            )
    return 0


def _load_local_skill(skill_db: str, skill_id: str):
    from .skills import LocalSkillStore

    store = LocalSkillStore(skill_db)
    try:
        skill = store.get(skill_id)
    finally:
        store.close()
    if skill is None:
        raise _CliError(f"skill not found: {skill_id}")
    return skill


def _cmd_skill_inspect(args, out) -> int:
    skill = _load_local_skill(args.skill_db, args.skill_id)
    out.write(skill.model_dump_json(indent=2) + "\n")
    return 0


def _cmd_skill_replay(args, out) -> int:
    if not args.dry_run:
        raise _CliError(
            "skill-replay currently requires --dry-run; execution is not enabled"
        )
    skill = _load_local_skill(args.skill_db, args.skill_id)
    violated = [
        item.id for item in skill.preconditions if item.status.value == "violated"
    ]
    plan = {
        "mode": "dry-run",
        "skill_id": skill.id,
        "name": skill.name,
        "blocked": bool(violated),
        "violated_preconditions": violated,
        "actions": [
            {
                "adapter": action.adapter,
                "operation": action.operation,
                "parameters": action.parameters,
            }
            for action in skill.actions
        ],
        "validators": [item.id for item in skill.validators],
        "note": "No executor was invoked.",
    }
    out.write(json.dumps(plan, indent=2) + "\n")
    return 0


def _cmd_skill_record(args, out) -> int:
    from .skills import (
        CliExecutionPolicy,
        CliSkillRecorder,
        LocalSkillStore,
        WorkspaceProbe,
    )

    if not args.approve_write:
        raise _CliError("skill-record requires --approve-write")
    command = list(args.argv)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise _CliError("skill-record requires a command after '--'")
    try:
        policy = CliExecutionPolicy.create(
            workspace=args.workspace,
            allowed_executables=args.allow_executable,
            timeout_s=args.timeout,
        )
        skill, demonstration = CliSkillRecorder(
            policy, WorkspaceProbe(args.workspace)
        ).record(
            command,
            name=args.name,
            description=args.description,
            approved_write=True,
        )
        store = LocalSkillStore(args.skill_db)
        try:
            store.save(skill)
        finally:
            store.close()
    except (OSError, RuntimeError, PermissionError, ValueError) as exc:
        raise _CliError(str(exc)) from exc
    out.write(
        json.dumps(
            {
                "skill_id": skill.id,
                "demonstration_id": demonstration.id,
                "status": "recorded",
                "actions": len(skill.actions),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


def _cmd_skill_run(args, out) -> int:
    from .skills import (
        CliActionExecutor,
        CliExecutionPolicy,
        LocalExecutionStore,
        SafeSkillRuntime,
        StaticApprovalProvider,
        WorkspaceConstraintValidator,
        WorkspaceProbe,
    )

    skill = _load_local_skill(args.skill_db, args.skill_id)
    try:
        policy = CliExecutionPolicy.create(
            workspace=args.workspace,
            allowed_executables=args.allow_executable,
            timeout_s=args.timeout,
        )
        ledger = LocalExecutionStore(args.skill_db)
        try:
            outcome = SafeSkillRuntime(
                executors={"cli": CliActionExecutor(policy)},
                validators={"workspace": WorkspaceConstraintValidator()},
                probe=WorkspaceProbe(args.workspace),
                approval=StaticApprovalProvider(args.approve_write),
                outcomes=ledger,
            ).run(
                skill,
                idempotency_key=args.idempotency_key or uuid.uuid4().hex,
            )
        finally:
            ledger.close()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _CliError(str(exc)) from exc
    out.write(outcome.model_dump_json(indent=2) + "\n")
    return 0 if outcome.status.value == "succeeded" else 1


def _cmd_workflow_list(args, out) -> int:
    from .orchestrator import LocalRunStateStore

    store = LocalRunStateStore(args.workflow_db)
    try:
        runs = store.list()
    finally:
        store.close()
    if args.json:
        out.write(
            json.dumps(
                [
                    {
                        "run_id": run.run_id,
                        "intent": run.intent,
                        "phase": run.phase.value,
                        "paused": run.pause.kind.value if run.pause else None,
                        "updated_at": run.updated_at.isoformat(),
                    }
                    for run in runs
                ],
                indent=2,
            )
            + "\n"
        )
    elif not runs:
        out.write("No workflow runs.\n")
    else:
        for run in runs:
            waiting = f" (paused: {run.pause.kind.value})" if run.pause else ""
            out.write(f"{run.run_id}  {run.phase.value}{waiting}  {run.intent}\n")
    return 0


def _cmd_workflow_status(args, out) -> int:
    from .orchestrator import LocalRunStateStore

    store = LocalRunStateStore(args.workflow_db)
    try:
        run = store.get(args.run_id)
    finally:
        store.close()
    if run is None:
        raise _CliError(f"workflow run not found: {args.run_id}")
    if args.json:
        out.write(run.model_dump_json(indent=2) + "\n")
        return 0
    out.write(f"run    : {run.run_id}\n")
    out.write(f"intent : {run.intent}\n")
    out.write(f"phase  : {run.phase.value}\n")
    if run.pause is not None:
        out.write(f"paused : {run.pause.kind.value} — {run.pause.prompt}\n")
    out.write("history:\n")
    for transition in run.history:
        out.write(
            f"  {transition.from_phase.value} -> {transition.to_phase.value}"
            f"  ({transition.note})\n"
        )
    return 0


def _cmd_skill_register(args, out) -> int:
    from .config import Settings
    from .skills.pack import load_skill_pack, load_starter_pack
    from .skills.registry import SkillRegistry

    if not args.starter and not args.pack:
        raise _CliError("provide a PACK path or --starter")
    settings = Settings.load(args.config)
    registry = SkillRegistry(args.registry or settings.skills.registry_path)
    try:
        registered = (
            load_starter_pack(registry)
            if args.starter
            else load_skill_pack(registry, args.pack)
        )
    finally:
        registry.close()
    if args.json:
        out.write(
            json.dumps(
                [
                    {"skill_id": r.skill_id, "semver": r.semver, "name": r.name}
                    for r in registered
                ]
            )
            + "\n"
        )
    else:
        for r in registered:
            out.write(f"{r.skill_id}@{r.semver}  {r.name}\n")
        out.write(f"registered {len(registered)} skill(s)\n")
    return 0


def _browser_record_session(*, intent, url, headless, audit_db, wait):
    from .skills.browser import discover_chromium
    from .skills.browser_observer import BrowserObserver
    from .skills.recorder import DemonstrationRecorder, DurableAuditLogSource

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise _CliError(
            "playwright not installed (`pip install 'oolu[browser]'`)"
        ) from exc

    conn = None
    log_source = None
    if audit_db:
        from .durable.audit import DurableAuditLog
        from .durable.connection import DurableConnection

        conn = DurableConnection(audit_db)
        log_source = DurableAuditLogSource(DurableAuditLog(conn))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless, executable_path=discover_chromium()
        )
        try:
            page = browser.new_page()
            observer = BrowserObserver()
            observer.attach(page)
            recorder = DemonstrationRecorder(observer, log_source=log_source)
            page.goto(url)
            recorder.start()
            wait()
            return recorder.stop(intent=intent, application="web")
        finally:
            browser.close()
            if conn is not None:
                conn.close()


def _cmd_record(args, out, *, session=None) -> int:
    from .config import Settings
    from .skills.learner import SkillLearner
    from .skills.registry import SkillRegistry

    settings = Settings.load(args.config)
    registry = SkillRegistry(args.registry or settings.skills.registry_path)

    if session is None:

        def session(*, intent, url, headless, audit_db):
            def wait():
                out.write(
                    "recording — interact with the browser, then press Enter here "
                    "to stop…\n"
                )
                out.flush()
                input()

            return _browser_record_session(
                intent=intent, url=url, headless=headless, audit_db=audit_db, wait=wait
            )

    try:
        recording = session(
            intent=args.intent,
            url=args.url,
            headless=args.headless,
            audit_db=args.audit_db,
        )
        from .naming import concise_name

        learned = SkillLearner(registry, scrub_pii=True).learn(
            recording.demonstration,
            # A name is a label, not a transcript: without an explicit
            # --name the skill is named by the intent's keywords, and the
            # full sentence lives on as the description.
            name=args.name or concise_name(args.intent),
            description=args.intent,
            adapter="browser",
            mode="actions",
            # A browser replay can have side effects; the draft is verified later.
            verify=False,
        )
    finally:
        registry.close()

    if args.json:
        out.write(
            json.dumps(
                {
                    "status": learned.status,
                    "skill_id": learned.registered.skill_id
                    if learned.registered
                    else None,
                    "actions": len(recording.demonstration.actions),
                    "metrics": recording.metrics.model_dump(),
                }
            )
            + "\n"
        )
    elif learned.registered is not None:
        out.write(
            f"{learned.status}: {learned.registered.skill_id}@{learned.registered.semver} "
            f"({len(recording.demonstration.actions)} actions, "
            f"{recording.metrics.duration_s:.1f}s) — unverified draft\n"
        )
    else:
        out.write(f"{learned.status}: {learned.reason}\n")
    return 0 if learned.status == "registered" else 1


def _cmd_serve(args, out) -> int:
    from .config import Settings
    from .skills.registry import SkillRegistry
    from .skills.server import SkillsServer

    settings = Settings.load(args.config)
    registry_path = args.registry or settings.skills.registry_path
    registry = SkillRegistry(registry_path)

    if args.seed_starter and not registry.list(limit=1):
        from .skills.pack import load_starter_pack

        load_starter_pack(registry)

    executors = {}
    tools = []
    if args.discover_tools:
        from .skills.discovery import discover_tools

        tools = discover_tools()
    if args.allow_executable or args.discover_tools:
        if not args.workspace:
            raise _CliError("--workspace is required to execute CLI tools")
        from .assembly import build_cli_executor

        allowed = list(args.allow_executable) + [t.path for t in tools]
        if allowed:
            executors.update(
                build_cli_executor(
                    workspace=args.workspace, allowed_executables=allowed
                )
            )
    if args.browser:
        from .assembly import build_browser_executor

        executors.update(build_browser_executor(allow_hosts=args.allow_host))

    app = SkillsServer(registry, executors=executors, tools=tools)
    try:
        import uvicorn
    except ImportError as exc:
        raise _CliError(
            "uvicorn not installed (`pip install 'oolu[serve]'`)"
        ) from exc

    out.write(
        f"serving /v1/skills on http://{args.host}:{args.port} "
        f"(registry: {registry_path})\n"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _cmd_desktop(args, out) -> int:
    """The desktop shell: the unified gateway surface, bound to loopback.

    The same multi-tenant gateway `oolu host` serves — same routes, same
    identity semantics, but the product front-end (the OoLu messenger,
    the built React shell) instead of the host-admin page — with a local
    user
    auto-provisioned and signed in: the browser opens straight into the
    shell, no sign-in screen, because on this machine the loopback bind
    (OS ownership), not a password, is the trust boundary.

    Ephemeral credentials per start are deliberate: the signing secret,
    the local user's password, and the auto-auth link are all re-minted
    on every launch, so nothing durable can leak. The DATA still lives
    in one directory and survives restarts like any host.
    """
    import secrets as secrets_module
    from pathlib import Path

    from .assembly import build_host_runtime
    from .config import Settings

    if args.host not in _LOOPBACK_HOSTS:
        raise _CliError(
            f"--host {args.host} is not loopback; the desktop shell is "
            "127.0.0.1-only — use `oolu host` to serve other machines"
        )

    data_dir = (
        Path(args.db).parent / "unified" if args.db else Path(".oolu/unified")
    )
    # --registry / --seed-starter keep their meaning: the starter pack
    # loads into the skill registry and its skills plan `POST /v1/runs`
    # intents — the setup scripts and the packaged app work unchanged.
    skills = None
    if args.registry:
        from .skills.registry import SkillRegistry

        registry = SkillRegistry(args.registry)
        try:
            if args.seed_starter and not registry.list(limit=1):
                from .skills.pack import load_starter_pack

                load_starter_pack(registry)
            skills = [entry.skill for entry in registry.list()] or None
        finally:
            registry.close()
    from .assembly import build_desktop_hands
    from .gateway import GatewayConfig

    runtime = build_host_runtime(
        Settings.load(args.config),
        data_dir=data_dir,
        secret=secrets_module.token_urlsafe(32),
        skills=skills,
        # The engine's hands on this machine: GET-only HTTP behind the
        # always-on SSRF guard, plus the LOCAL DEVICE's own command line —
        # the discovered tools (ffmpeg, pandoc, …), workspace-confined.
        # OOLU_CLI_TOOLS=off disables the CLI hand; OOLU_CLI_ALLOWLIST
        # widens it; OOLU_HTTP_ALLOWLIST / OOLU_HTTP_ALLOW_PRIVATE keep
        # their meaning for the HTTP hand.
        executors=build_desktop_hands(data_dir=data_dir),
        # Edge means the user's OWN machine: the chat can find files on
        # this computer (find_local_files, home-rooted, listing only).
        # `oolu host` never passes this — a server stays out of homes.
        local_files_root=Path.home(),
        # A desktop session should outlive a workday without re-auth.
        token_ttl_seconds=7 * 24 * 3600,
        # The product face: the OoLu messenger (built React shell), not
        # the multi-user admin page `oolu host` serves.
        frontend="shell",
        # The online server this app pairs with: the sign-in screen uses
        # it instead of asking the user to type a server. The desktop's
        # home tenant is "local" — planning reads its key and settings.
        config=GatewayConfig(
            server_url=os.environ.get("OOLU_SERVER_URL"),
            registration_tenant="local",
        ),
        # "Continue with Google" turns on when the operator provides a
        # Google OAuth client (Desktop-app type; the id is not a secret).
        google_client_id=os.environ.get("OOLU_GOOGLE_CLIENT_ID"),
        google_client_secret=os.environ.get("OOLU_GOOGLE_CLIENT_SECRET", ""),
        google_default_tenant="local",
        # The prebuilt functions above, packaged as one Work node
        # ("Handiwork") owned by this machine's local user.
        seed_handiwork_for="local",
    )
    password = secrets_module.token_urlsafe(16)
    if not runtime.accounts.bootstrap(
        tenant="local", username="local", password=password
    ):
        # Second launch: the user exists with last launch's (discarded)
        # password. Rotate it to this launch's — same trust boundary.
        runtime.accounts.change_password("local", password)
    login = runtime.accounts.login("local", password)

    # The machine's own brain, if this machine can host one: pull the
    # default local model (qwen3:4b — the representative's QLoRA family)
    # into Ollama in the background. Best-effort by design: no Ollama,
    # no network, no problem — the shell serves either way, and the user
    # can point model.local_model anywhere else in Settings.
    import threading

    from .providers.localmodel import DEFAULT_LOCAL_MODEL, ensure_default_local_model

    def _pull_default_model() -> None:
        state = ensure_default_local_model()
        if state in ("pulled", "present"):
            out.write(
                f"local model {DEFAULT_LOCAL_MODEL} {state} — set the"
                " default model to 'local' in Settings to use it\n"
            )

    threading.Thread(target=_pull_default_model, daemon=True).start()

    try:
        import uvicorn
    except ImportError as exc:
        runtime.close()
        raise _CliError(
            "uvicorn not installed (`pip install 'oolu[serve]'`)"
        ) from exc

    url = f"http://{args.host}:{args.port}/#auth={login.token}"
    out.write(
        "OoLu shell is starting (unified surface).\n"
        f"  open {url}\n"
        f"  (data: {data_dir}; signed in automatically as 'local'"
        + (f"; {len(skills)} skills loaded" if skills else "")
        + ")\n"
        "  press Ctrl+C to stop\n"
    )
    if args.open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, webbrowser.open, [url]).start()
    try:
        uvicorn.run(runtime.asgi, host=args.host, port=args.port, log_level="info")
    finally:
        runtime.close()
    return 0


def _cmd_host(args, out) -> int:
    """Multi-user web hosting: the full multi-tenant gateway, local accounts.

    Unlike `oolu desktop` (one auto signed-in local user), every person
    gets their own username, password, and authority — the same identity
    semantics as an IdP-fronted deployment, with this install signing its
    own tokens.
    """
    import secrets as secrets_module

    from .assembly import build_host_runtime
    from .config import Settings

    secret = os.environ.get(args.secret_env)
    if secret is None and args.secret_env == "OOLU_HOST_SECRET":
        # Backwards-compatible fallback to the pre-rebrand variable name.
        secret = os.environ.get("WFGPS_HOST_SECRET")
    ephemeral_secret = secret is None
    if ephemeral_secret:
        secret = secrets_module.token_urlsafe(32)
    from .gateway import GatewayConfig

    config = GatewayConfig(
        allowed_origins=frozenset(args.allow_origin),
        open_registration=args.open_registration,
        registration_tenant=args.tenant,
        server_url=os.environ.get("OOLU_SERVER_URL"),
        global_service=args.global_service,
    )
    from .mail import build_mail_sender
    from .sms import build_sms_sender

    mail = build_mail_sender(os.environ)
    # The phone door: OOLU_SMS=console for development, OOLU_SMS_URL +
    # OOLU_SMS_KEY + OOLU_SMS_FROM for a real provider; absent, the
    # phone routes answer 404 and the app hides the button.
    sms = build_sms_sender(os.environ)
    if args.global_service and args.open_registration and mail is None:
        raise _CliError(
            "--global-service with --open-registration needs a mail sender:"
            " strangers must prove their e-mail address before they get an"
            " account. Set OOLU_MAIL_URL + OOLU_MAIL_KEY + OOLU_MAIL_FROM"
            " (or OOLU_MAIL=console for a dry run)."
        )
    stripe_secret_key = os.environ.get("OOLU_STRIPE_KEY", "").strip() or None
    stripe_webhook_secret = (
        os.environ.get("OOLU_STRIPE_WEBHOOK_SECRET", "").strip() or None
    )
    if args.transactions and stripe_secret_key is None:
        raise _CliError(
            "--transactions opens the port real cards are charged through,"
            " so it refuses to open onto test doubles. Set OOLU_STRIPE_KEY"
            " (and OOLU_STRIPE_WEBHOOK_SECRET for refund/payout events)."
        )
    # The hosted plan's brain: platform keys are the operator's, from the
    # environment only — never a request, never a file in the repo.
    platform_model_keys = {
        "anthropic": os.environ.get("OOLU_PLATFORM_ANTHROPIC_KEY", ""),
        "openai": os.environ.get("OOLU_PLATFORM_OPENAI_KEY", ""),
    }
    # Two doors, one gateway: OOLU_ADMIN_HOST names the hostname(s) —
    # comma-separated — whose requests get the operator's admin page;
    # every other Host serves the product shell (the messenger users
    # chat in). Unset keeps the classic single-face admin host.
    admin_hosts = tuple(
        h.strip()
        for h in os.environ.get("OOLU_ADMIN_HOST", "").split(",")
        if h.strip()
    )
    try:
        runtime = build_host_runtime(
            Settings.load(args.config),
            data_dir=args.data,
            secret=secret,
            database_url=args.database_url,
            frontend="shell" if admin_hosts else "host",
            shell_remote=True,
            admin_hosts=admin_hosts,
            config=config,
            google_client_id=os.environ.get("OOLU_GOOGLE_CLIENT_ID"),
            google_client_secret=os.environ.get("OOLU_GOOGLE_CLIENT_SECRET", ""),
            google_default_tenant=args.tenant,
            mail=mail,
            sms=sms,
            require_isolation=args.global_service,
            platform_model_keys=platform_model_keys,
            transactions_enabled=args.transactions,
            ordering_enabled=args.ordering,
            stripe_secret_key=stripe_secret_key,
            stripe_webhook_secret=stripe_webhook_secret,
        )
    except ValueError as exc:
        raise _CliError(str(exc)) from exc

    admin_password = os.environ.get(args.admin_password_env)
    if admin_password is None and args.admin_password_env == "OOLU_ADMIN_PASSWORD":
        # Backwards-compatible fallback to the pre-rebrand variable name.
        admin_password = os.environ.get("WFGPS_ADMIN_PASSWORD")
    generated_password = admin_password is None
    if generated_password:
        admin_password = secrets_module.token_urlsafe(12)
    created = runtime.accounts.bootstrap(
        tenant=args.tenant, username=args.admin, password=admin_password
    )

    try:
        import uvicorn
    except ImportError as exc:
        runtime.close()
        raise _CliError(
            "uvicorn not installed (`pip install 'oolu[serve]'`)"
        ) from exc

    shown = "<this-host>" if args.host in ("0.0.0.0", "::") else args.host
    database = "postgres (online)" if args.database_url else f"sqlite ({args.data})"
    out.write(
        f"OoLu multi-user host is starting on {args.host}:{args.port}.\n"
        f"  data    : {args.data}\n"
        f"  database: {database}\n"
        + (
            f"  faces   : the app shell everywhere; the admin console at "
            f"{', '.join(admin_hosts)}\n"
            if admin_hosts
            else ""
        )
        + f"  sign in : POST http://{shown}:{args.port}/v1/auth/login "
        '{"username": "%s", "password": "..."}\n' % args.admin
    )
    if created and generated_password:
        out.write(
            f"  admin   : {args.admin} / {admin_password}   "
            "(shown ONCE — change it, or set "
            f"{args.admin_password_env} before first start)\n"
        )
    elif not created:
        out.write(f"  admin   : {args.admin} already exists (password unchanged)\n")
    if ephemeral_secret:
        out.write(
            f"  NOTE: {args.secret_env} is not set, so the token-signing secret\n"
            "  is ephemeral — every sign-in dies with this process. Set it for\n"
            "  logins that survive restarts.\n"
        )
    out.write(
        "  IMPORTANT: expose this only behind HTTPS (a reverse proxy such as\n"
        "  Caddy or nginx) — passwords and tokens travel with every request.\n"
        "  press Ctrl+C to stop\n"
    )
    try:
        uvicorn.run(runtime.asgi, host=args.host, port=args.port, log_level="info")
    finally:
        runtime.close()
    return 0


def _version() -> str:
    return __version__


# --------------------------------------------------------------------------- #
# Entry point.                                                                 #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None, *, builder=None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            # Preflight guards the real engine only: an injected builder is
            # a test/embedding context that brings its own model stack.
            preflight = builder is None and not args.no_preflight
            if builder is None:
                from .config import build_oolu

                builder = build_oolu
            return _cmd_run(args, builder, out, preflight=preflight)
        if args.command == "record":
            return _cmd_record(args, out)
        if args.command == "skill-register":
            return _cmd_skill_register(args, out)
        if args.command == "serve":
            return _cmd_serve(args, out)
        if args.command == "desktop":
            return _cmd_desktop(args, out)
        if args.command == "host":
            return _cmd_host(args, out)
        if args.command == "show-config":
            return _cmd_show_config(args, out)
        if args.command == "doctor":
            return _cmd_doctor(args, out)
        if args.command == "backup":
            return _cmd_backup(args, out)
        if args.command == "telegram":
            return _cmd_telegram(args)
        if args.command == "reply-teach":
            return _cmd_reply_teach(args, out)
        if args.command == "skill-list":
            return _cmd_skill_list(args, out)
        if args.command == "skill-inspect":
            return _cmd_skill_inspect(args, out)
        if args.command == "skill-replay":
            return _cmd_skill_replay(args, out)
        if args.command == "skill-record":
            return _cmd_skill_record(args, out)
        if args.command == "skill-run":
            return _cmd_skill_run(args, out)
        if args.command == "workflow-list":
            return _cmd_workflow_list(args, out)
        if args.command == "workflow-status":
            return _cmd_workflow_status(args, out)
        if args.command == "representative-status":
            return _cmd_representative_status(args, out)
        if args.command == "representative-train":
            return _cmd_representative_train(args, out)
        if args.command == "version":
            out.write(f"oolu {_version()}\n")
            return 0
    except _CliError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
