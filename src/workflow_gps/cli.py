"""Command-line entry point for Workflow-GPS.

    wfgps run "convert sales.csv into a bar chart"
    wfgps run "slugify a title" --backend docker --knowledge local
    wfgps run "..." --json                 # machine-readable result
    wfgps show-config                       # print effective settings
    wfgps version

Stdlib argparse only. The engine builder is injectable (``builder=``) so the CLI is
testable without a live vLLM/litellm stack; in production it defaults to the real
``build_workflow_gps`` which talks to the configured OpenAI-compatible endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .config import Settings, build_workflow_gps
from .telemetry import configure_logging, get_logger, render_result

_DEFAULT_KNOWLEDGE_DB = os.path.expanduser("~/.workflow-gps/knowledge.db")


class _CliError(Exception):
    """User-facing configuration/usage error (exit code 2)."""


# --------------------------------------------------------------------------- #
# Argument parser.                                                            #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wfgps", description="Workflow-GPS — self-healing local agent engine.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run an intent through the engine")
    run.add_argument("intent", help="the task to accomplish, in natural language")
    run.add_argument("--config", metavar="PATH", help="path to a models.yaml settings file")
    run.add_argument("--backend", choices=["subprocess", "docker"], help="override the execution backend")
    run.add_argument("--knowledge", choices=["none", "local", "remote"], default="none",
                     help="knowledge layer (default: none)")
    run.add_argument("--knowledge-db", metavar="PATH", default=_DEFAULT_KNOWLEDGE_DB,
                     help="SQLite path for local/remote cache")
    run.add_argument("--log-level", default="INFO",
                     choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="console log level")
    run.add_argument("--json", action="store_true", help="emit the result as JSON instead of a panel")

    sub.add_parser("show-config", help="print the effective settings").add_argument(
        "--config", metavar="PATH", help="path to a models.yaml settings file")
    sub.add_parser("version", help="print the version")
    return parser


# --------------------------------------------------------------------------- #
# Settings / knowledge wiring.                                                #
# --------------------------------------------------------------------------- #
def _load_settings(args) -> Settings:
    # Settings.load() resolves --config (or $WFGPS_CONFIG, else defaults) AND layers
    # the WFGPS_* env overrides on top. Calling Settings() directly would skip those
    # overrides, so always go through load() even when no config path is given.
    settings = Settings.load(getattr(args, "config", None))
    if getattr(args, "backend", None):
        settings = settings.model_copy(update={
            "backend": settings.backend.model_copy(update={"kind": args.backend})
        })
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
            raise _CliError("--knowledge remote needs WFGPS_KNOWLEDGE_URL and WFGPS_KNOWLEDGE_TOKEN")
        from .knowledge import RemoteConfig, RemoteKnowledgeClient, StaticTokenProvider

        return RemoteKnowledgeClient(RemoteConfig(base_url=url), StaticTokenProvider(token),
                                     local_db_path=db_path)
    raise _CliError(f"unknown knowledge kind: {kind}")


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
    }


def _cmd_run(args, builder, out) -> int:
    configure_logging(level=args.log_level)
    log = get_logger("cli")
    settings = _load_settings(args)
    knowledge = _build_knowledge(args.knowledge, args.knowledge_db)
    log.info("navigating: %s", args.intent)

    engine = builder(settings, knowledge=knowledge)
    try:
        result = engine.run(args.intent)
    finally:
        if knowledge is not None and hasattr(knowledge, "close"):
            knowledge.close()  # flushes best-effort remote uploads

    if args.json:
        out.write(json.dumps(_result_to_dict(result), indent=2) + "\n")
    else:
        render_result(result)
    return 0 if getattr(result, "success", False) else 1


def _cmd_show_config(args, out) -> int:
    s = _load_settings(args)
    out.write("Workflow-GPS effective settings:\n")
    out.write(f"  fast tier      : {s.routing.fast.model} @ {s.routing.fast.api_base}\n")
    out.write(f"  reasoning tier : {s.routing.reasoning.model} @ {s.routing.reasoning.api_base}\n")
    out.write(f"  backend        : {s.backend.kind}\n")
    out.write(f"  max recalcs    : {s.edges.max_recalcs}\n")
    out.write(f"  limits         : mem={s.limits.memory_mb}MB wall={s.limits.wall_timeout_s}s "
              f"install={s.limits.install_timeout_s}s read_only_rootfs={s.limits.read_only_rootfs}\n")
    return 0


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("workflow-gps")
    except Exception:  # noqa: BLE001 - not installed as a distribution
        return "0.1.0-dev"


# --------------------------------------------------------------------------- #
# Entry point.                                                                 #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None, *, builder=build_workflow_gps, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args, builder, out)
        if args.command == "show-config":
            return _cmd_show_config(args, out)
        if args.command == "version":
            out.write(f"workflow-gps {_version()}\n")
            return 0
    except _CliError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
