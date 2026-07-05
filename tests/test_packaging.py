"""The native-app packaging story stays honest.

The frozen binary is a thin wrapper over ``wfgps desktop`` — one launch
path shared with the setup scripts. These tests pin the links: the
launcher's argv parses against the real CLI, the port fallback works, the
spec bundles what the frozen app actually reads at runtime, and the CI
workflow builds with the same script a human would run.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

import shell_launcher  # noqa: E402


def test_launcher_argv_parses_against_the_real_cli(tmp_path):
    from workflow_gps.cli import build_parser

    argv = shell_launcher.desktop_argv(tmp_path, 8765)
    args = build_parser().parse_args(argv)
    assert args.command == "desktop"
    assert args.seed_starter and args.open_browser
    assert args.port == 8765
    assert args.host == "127.0.0.1"  # the frozen app stays loopback-only
    assert Path(args.db).parent == tmp_path  # user data under the data dir
    assert Path(args.registry).parent == tmp_path


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
    spec = (ROOT / "packaging" / "wfgps_shell.spec").read_text()
    # The starter pack rides importlib.resources: data files must be
    # collected, and uvicorn's dynamic imports must be spelled out.
    assert 'collect_data_files("workflow_gps")' in spec
    assert "uvicorn.protocols.http.h11_impl" in spec
    assert '"shell_launcher.py"' in spec
    assert 'name="WorkflowGPS-Shell"' in spec
    # The heavy optional stacks stay out of the binary.
    for excluded in ("langgraph", "litellm", "docker", "playwright", "psycopg"):
        assert f'"{excluded}"' in spec


def test_ci_workflow_builds_with_the_same_script():
    workflow = (ROOT / ".github" / "workflows" / "build-installers.yml").read_text()
    assert "python packaging/build_installer.py" in workflow
    for runner in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert runner in workflow
    assert "dist/WorkflowGPS-Shell" in workflow


def test_readme_documents_the_native_app():
    readme = (ROOT / "README.md").read_text()
    assert "packaging/build_installer.py" in readme
    assert "WorkflowGPS-Shell" in readme
