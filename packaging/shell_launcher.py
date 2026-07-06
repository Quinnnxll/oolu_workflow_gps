"""Standalone entry point for the packaged OoLu shell.

This is the file PyInstaller freezes into the single-file app. It is a thin,
argument-free wrapper over the same ``oolu desktop`` command a developer
runs — double-clicking the executable must behave exactly like the setup
scripts, so there is one launch path to keep honest:

- user data lives in ``~/.oolu`` (the executable can run from a
  Downloads folder, a USB stick, anywhere — data stays put across moves);
- the port prefers 8765 but falls back to any free one, so a second copy
  or another app on the port never turns into an error dialog;
- the browser auto-opens and the starter skill library is seeded.

Double-click safety net: a frozen ``console=True`` app closes its console
window the instant the process exits, so any startup crash would otherwise
be an unreadable ~1-second flash for a non-developer. ``main`` therefore
catches every failure, writes it to ``~/.oolu/shell-crash.log``, and
holds the window open with a prompt — the app never silently vanishes.

A few environment variables make the frozen app controllable (and testable
in CI) without changing the double-click default behaviour:

- ``OOLU_SHELL_OPEN_BROWSER=0``  do not auto-open the browser;
- ``OOLU_SHELL_PORT=<n>``        bind a fixed port instead of auto-picking;
- ``OOLU_SHELL_DATA_DIR=<path>`` use a different data directory;
- ``OOLU_SHELL_NO_HOLD=1``       never pause the console on error (CI).
"""

from __future__ import annotations

import os
import socket
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

PREFERRED_PORT = 8765
CRASH_LOG_NAME = "shell-crash.log"


def free_port(preferred: int = PREFERRED_PORT) -> int:
    """The preferred port if it is free, else one the OS hands out."""
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", preferred))
            return preferred
    except OSError:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


def desktop_argv(data_dir: Path, port: int, *, open_browser: bool = True) -> list[str]:
    """The exact `oolu desktop` invocation the packaged app performs."""
    argv = [
        "desktop",
        "--db",
        str(data_dir / "desktop.db"),
        "--registry",
        str(data_dir / "skills.db"),
        "--seed-starter",
        "--port",
        str(port),
    ]
    if open_browser:
        argv.append("--open")
    return argv


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean-ish environment variable (unset -> ``default``)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _migrate_legacy_data(base: Path) -> None:
    """One-time move of a pre-rebrand ``~/.workflow-gps`` tree to ``~/.oolu``.

    Only runs when the new location does not exist yet, so an existing OoLu
    install is never touched and the migration happens at most once. A locked
    or cross-device move is non-fatal — the app just starts with a fresh dir.
    """
    if base.exists():
        return
    legacy = base.parent / ".workflow-gps"
    if not legacy.is_dir():
        return
    try:
        legacy.rename(base)
    except OSError:
        pass


def data_dir() -> Path:
    """The durable data directory, honouring ``OOLU_SHELL_DATA_DIR``."""
    override = os.environ.get("OOLU_SHELL_DATA_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        base = Path.home() / ".oolu"
        _migrate_legacy_data(base)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _resolve_port() -> int:
    override = os.environ.get("OOLU_SHELL_PORT")
    if override:
        return int(override)
    return free_port()


def _run() -> int:
    from oolu.cli import main as oolu_main

    open_browser = _env_flag("OOLU_SHELL_OPEN_BROWSER", True)
    return oolu_main(
        desktop_argv(data_dir(), _resolve_port(), open_browser=open_browser)
    )


def _write_crash_log(text: str) -> Path | None:
    """Persist the traceback so the failure survives the console closing."""
    try:
        path = data_dir() / CRASH_LOG_NAME
        stamp = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {stamp} =====\n{text}\n")
        return path
    except OSError:
        return None


def _should_hold() -> bool:
    """Hold the window only for an interactive frozen double-click.

    Never pause when output is redirected (CI, pipes) or ``OOLU_SHELL_NO_HOLD``
    is set, so automated runs cannot hang waiting on ``input``.
    """
    if _env_flag("OOLU_SHELL_NO_HOLD", False):
        return False
    if not getattr(sys, "frozen", False):
        return False
    stdin = sys.stdin
    return bool(stdin is not None and stdin.isatty())


def _report_crash(text: str) -> None:
    log_path = _write_crash_log(text)
    sys.stderr.write("\nOoLu could not start.\n\n")
    sys.stderr.write(text)
    if log_path is not None:
        sys.stderr.write(f"\nThis error was saved to:\n  {log_path}\n")
    sys.stderr.flush()
    if _should_hold():
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, RuntimeError):
            pass


def main() -> int:
    try:
        return _run()
    except KeyboardInterrupt:
        # Ctrl+C is the documented stop control — a clean exit, not a crash.
        return 0
    except SystemExit as exc:  # uvicorn calls sys.exit() on a failed bind
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code:
            _report_crash(f"the shell exited with status {code}\n")
        return code
    except BaseException:  # noqa: BLE001 — the last line of defence for the UX
        _report_crash(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
