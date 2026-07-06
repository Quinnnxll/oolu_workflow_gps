"""The native-app packaging story stays honest.

The frozen binary is a thin wrapper over ``oolu desktop`` — one launch
path shared with the setup scripts. These tests pin the links: the
launcher's argv parses against the real CLI, the port fallback works, the
spec bundles what the frozen app actually reads at runtime, and the CI
workflow builds with the same script a human would run.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

import shell_launcher  # noqa: E402


def test_launcher_argv_parses_against_the_real_cli(tmp_path):
    from oolu.cli import build_parser

    argv = shell_launcher.desktop_argv(tmp_path, 8765)
    args = build_parser().parse_args(argv)
    assert args.command == "desktop"
    assert args.seed_starter and args.open_browser
    assert args.port == 8765
    assert args.host == "127.0.0.1"  # the frozen app stays loopback-only
    assert Path(args.db).parent == tmp_path  # user data under the data dir
    assert Path(args.registry).parent == tmp_path


def test_open_browser_can_be_disabled_by_env(tmp_path):
    from oolu.cli import build_parser

    argv = shell_launcher.desktop_argv(tmp_path, 8765, open_browser=False)
    args = build_parser().parse_args(argv)
    assert args.open_browser is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("0", False), ("false", False), ("no", False), ("1", True)],
)
def test_env_flag_reads_boolean_ish(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("OOLU_SHELL_OPEN_BROWSER", raising=False)
    else:
        monkeypatch.setenv("OOLU_SHELL_OPEN_BROWSER", value)
    assert shell_launcher._env_flag("OOLU_SHELL_OPEN_BROWSER", True) is expected


def test_data_dir_honours_override(monkeypatch, tmp_path):
    target = tmp_path / "custom-data"
    monkeypatch.setenv("OOLU_SHELL_DATA_DIR", str(target))
    assert shell_launcher.data_dir() == target
    assert target.is_dir()  # created on demand


def test_a_startup_crash_never_vanishes_silently(monkeypatch, tmp_path, capsys):
    # The whole point of the frozen launcher: a crash must be surfaced and
    # persisted, not flashed for a second and lost when the console closes.
    monkeypatch.setenv("OOLU_SHELL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OOLU_SHELL_NO_HOLD", "1")  # never block a test on input

    def _boom() -> int:
        raise RuntimeError("kaboom during startup")

    monkeypatch.setattr(shell_launcher, "_run", _boom)
    code = shell_launcher.main()

    assert code == 1
    crash_log = tmp_path / shell_launcher.CRASH_LOG_NAME
    assert crash_log.is_file()
    assert "kaboom during startup" in crash_log.read_text()
    assert "kaboom during startup" in capsys.readouterr().err


def test_keyboard_interrupt_is_a_clean_stop(monkeypatch, tmp_path):
    monkeypatch.setenv("OOLU_SHELL_DATA_DIR", str(tmp_path))

    def _stop() -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(shell_launcher, "_run", _stop)
    assert shell_launcher.main() == 0
    assert not (tmp_path / shell_launcher.CRASH_LOG_NAME).exists()


def test_free_port_prefers_then_falls_back():
    port = shell_launcher.free_port()
    assert port == shell_launcher.PREFERRED_PORT
    # Occupy the preferred port: the launcher must yield another, silently.
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", shell_launcher.PREFERRED_PORT))
        blocker.listen(1)
        fallback = shell_launcher.free_port()
        assert fallback != shell_launcher.PREFERRED_PORT
        assert fallback > 0


def test_spec_bundles_what_the_frozen_app_reads():
    spec = (ROOT / "packaging" / "oolu_shell.spec").read_text()
    # The starter pack rides importlib.resources: data files must be
    # collected, and uvicorn's dynamic imports must be spelled out.
    assert 'collect_data_files("oolu")' in spec
    assert "uvicorn.protocols.http.h11_impl" in spec
    assert '"shell_launcher.py"' in spec
    assert 'name="OoLu-Shell"' in spec
    # The heavy optional stacks stay out of the binary.
    for excluded in ("langgraph", "litellm", "docker", "playwright", "psycopg"):
        assert f'"{excluded}"' in spec


def test_ci_workflow_builds_with_the_same_script():
    workflow = (ROOT / ".github" / "workflows" / "build-installers.yml").read_text()
    assert "python packaging/build_installer.py" in workflow
    for runner in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert runner in workflow
    assert "dist/OoLu-Shell" in workflow


def test_readme_documents_the_native_app():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "packaging/build_installer.py" in readme
    assert "OoLu-Shell" in readme
