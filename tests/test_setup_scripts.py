"""The zip-download setup story stays honest.

A non-developer's path is README quickstart -> setup.bat / setup.sh ->
`oolu desktop --open`. These tests pin each link: the scripts exist and
are runnable, they install the right extra and launch the right command
with flags the CLI actually accepts, and the README points at them.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_setup_scripts_exist_and_are_runnable():
    bat = ROOT / "setup.bat"
    sh = ROOT / "setup.sh"
    assert bat.is_file() and sh.is_file()
    assert os.access(sh, os.X_OK), "setup.sh must be executable"
    assert sh.read_text().startswith("#!"), "setup.sh needs a shebang"


def test_setup_scripts_install_and_launch_the_same_way():
    for script in (ROOT / "setup.bat", ROOT / "setup.sh"):
        text = script.read_text()
        # The shell needs only the serve extra — never the heavy engine.
        assert '-e ".[serve]"' in text, f"{script.name} installs the wrong extra"
        assert "-m oolu.cli" in text and "desktop" in text
        # Non-developers get a seeded skill library and an auto-opened browser.
        assert "--seed-starter" in text and "--open" in text
        # Everything stays inside the folder.
        assert ".venv" in text
        # A friendly pointer when Python is missing.
        assert "python.org/downloads" in text


def test_cli_accepts_every_flag_the_scripts_pass():
    from oolu.cli import build_parser

    args = build_parser().parse_args(
        [
            "desktop",
            "--registry",
            ".oolu/skills.db",
            "--seed-starter",
            "--open",
        ]
    )
    assert args.command == "desktop"
    assert args.seed_starter and args.open_browser
    assert args.host == "127.0.0.1"  # loopback-only stays the default


def test_readme_quickstart_points_at_the_scripts():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "setup.bat" in readme and "setup.sh" in readme
    assert "Download ZIP" in readme
    assert "python.org/downloads" in readme
