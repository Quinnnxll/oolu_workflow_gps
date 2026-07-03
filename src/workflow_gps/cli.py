"""Command-line entry point for Workflow-GPS.

    wfgps run "convert sales.csv into a bar chart"
    wfgps run "slugify a title" --backend docker --knowledge local
    wfgps run "..." --json                 # machine-readable result
    wfgps record "book a flight" --url https://air.example  # learn a browser skill
    wfgps show-config                       # print effective settings
    wfgps telegram --reply-config replies.json
    wfgps version

Stdlib argparse only. The engine builder is injectable (``builder=``) so the CLI is
testable without a live vLLM/litellm stack; in production it defaults to the real
``build_workflow_gps`` which talks to the configured OpenAI-compatible endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid

from . import __version__
from .telemetry import configure_logging, get_logger, render_result

_DEFAULT_KNOWLEDGE_DB = os.path.expanduser("~/.workflow-gps/knowledge.db")
_DEFAULT_SCRIPT_CACHE_DB = os.path.expanduser("~/.workflow-gps/script-cache.db")
_DEFAULT_TELEGRAM_OFFSET = os.path.expanduser("~/.workflow-gps/telegram-offset.json")
_DEFAULT_REPLY_MEMORY_DB = os.path.expanduser("~/.workflow-gps/learned-replies.db")
_DEFAULT_SKILL_DB = os.path.expanduser("~/.workflow-gps/skills.db")
_DEFAULT_WORKFLOW_DB = os.path.expanduser("~/.workflow-gps/workflows.db")


class _CliError(Exception):
    """User-facing configuration/usage error (exit code 2)."""


# --------------------------------------------------------------------------- #
# Argument parser.                                                            #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wfgps", description="Workflow-GPS — self-healing local agent engine."
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

    sub.add_parser("show-config", help="print the effective settings").add_argument(
        "--config", metavar="PATH", help="path to a models.yaml settings file"
    )
    sub.add_parser("version", help="print the version")
    return parser


# --------------------------------------------------------------------------- #
# Settings / knowledge wiring.                                                #
# --------------------------------------------------------------------------- #
def _load_settings(args):
    # Settings.load() resolves --config (or $WFGPS_CONFIG, else defaults) AND layers
    # the WFGPS_* env overrides on top. Calling Settings() directly would skip those
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
        url = os.environ.get("WFGPS_KNOWLEDGE_URL")
        token = os.environ.get("WFGPS_KNOWLEDGE_TOKEN")
        if not (url and token):
            raise _CliError(
                "--knowledge remote needs WFGPS_KNOWLEDGE_URL and WFGPS_KNOWLEDGE_TOKEN"
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


def _cmd_run(args, builder, out) -> int:
    configure_logging(level=args.log_level)
    log = get_logger("cli")
    settings = _load_settings(args)
    knowledge = _build_knowledge(args.knowledge, args.knowledge_db)
    script_cache = _build_script_cache(args.script_cache, args.script_cache_db)
    log.info("navigating: %s", args.intent)

    builder_kwargs = {"knowledge": knowledge}
    if script_cache is not None:
        builder_kwargs["script_cache"] = script_cache
    try:
        engine = builder(settings, **builder_kwargs)
        result = engine.run(args.intent)
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
    out.write("Workflow-GPS effective settings:\n")
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
            "playwright not installed (`pip install 'workflow-gps[browser]'`)"
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
        learned = SkillLearner(registry, scrub_pii=True).learn(
            recording.demonstration,
            name=args.name or args.intent,
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
            "uvicorn not installed (`pip install 'workflow-gps[serve]'`)"
        ) from exc

    out.write(
        f"serving /v1/skills on http://{args.host}:{args.port} "
        f"(registry: {registry_path})\n"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
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
            if builder is None:
                from .config import build_workflow_gps

                builder = build_workflow_gps
            return _cmd_run(args, builder, out)
        if args.command == "record":
            return _cmd_record(args, out)
        if args.command == "skill-register":
            return _cmd_skill_register(args, out)
        if args.command == "serve":
            return _cmd_serve(args, out)
        if args.command == "show-config":
            return _cmd_show_config(args, out)
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
        if args.command == "version":
            out.write(f"workflow-gps {_version()}\n")
            return 0
    except _CliError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
