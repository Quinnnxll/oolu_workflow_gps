"""Standalone entry point for the packaged Workflow-GPS shell.

This is the file PyInstaller freezes into the single-file app. It is a thin,
argument-free wrapper over the same ``wfgps desktop`` command a developer
runs — double-clicking the executable must behave exactly like the setup
scripts, so there is one launch path to keep honest:

- user data lives in ``~/.workflow-gps`` (the executable can run from a
  Downloads folder, a USB stick, anywhere — data stays put across moves);
- the port prefers 8765 but falls back to any free one, so a second copy
  or another app on the port never turns into an error dialog;
- the browser auto-opens and the starter skill library is seeded.
"""

from __future__ import annotations

import socket
from pathlib import Path

PREFERRED_PORT = 8765


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
    """The exact `wfgps desktop` invocation the packaged app performs."""
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


def main() -> int:
    from workflow_gps.cli import main as wfgps_main

    data_dir = Path.home() / ".workflow-gps"
    data_dir.mkdir(parents=True, exist_ok=True)
    return wfgps_main(desktop_argv(data_dir, free_port()))


if __name__ == "__main__":
    raise SystemExit(main())
