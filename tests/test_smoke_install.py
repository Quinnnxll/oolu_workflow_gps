"""Fresh-environment installation and CLI smoke tests.

These verify the packaged install path end to end: the distribution exposes the
``oolu`` console entry point, the reported version is consistent across the
package constant, installed metadata, and the CLI, and the core read-only
commands run as a fresh subprocess (not just via an in-process ``main`` call).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from oolu import __version__

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# --------------------------------------------------------------------------- #
# Packaging: the distribution is installed and self-consistent.               #
# --------------------------------------------------------------------------- #
def test_distribution_is_installed():
    dist = importlib.metadata.distribution("oolu")
    assert dist.version == __version__


def test_console_script_entry_point_is_registered():
    eps = importlib.metadata.entry_points(group="console_scripts")
    scripts = {ep.name: ep.value for ep in eps}
    # `oolu` is the brand command; `oolu` stays as a backwards-compatible alias.
    assert scripts.get("oolu") == "oolu.cli:main"
    assert scripts.get("oolu") == "oolu.cli:main"


def test_persistence_package_is_importable():
    # Guards against the packaging config dropping a subpackage from the wheel.
    from oolu.persistence import migrate, schema_version  # noqa: F401


# --------------------------------------------------------------------------- #
# CLI smoke tests as a fresh subprocess.                                       #
# --------------------------------------------------------------------------- #
def test_module_invocation_reports_version():
    proc = _run([sys.executable, "-m", "oolu.cli", "version"])
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"oolu {__version__}"


def test_module_help_lists_core_commands():
    proc = _run([sys.executable, "-m", "oolu.cli", "--help"])
    assert proc.returncode == 0
    for command in ("run", "show-config", "version", "skill-list", "workflow-list"):
        assert command in proc.stdout


def test_module_show_config_runs():
    proc = _run([sys.executable, "-m", "oolu.cli", "show-config"])
    assert proc.returncode == 0
    assert "fast tier" in proc.stdout


@pytest.mark.skipif(
    shutil.which("oolu") is None,
    reason="oolu console script not on PATH in this environment",
)
def test_installed_console_script_runs():
    proc = _run(["oolu", "version"])
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"oolu {__version__}"


# --------------------------------------------------------------------------- #
# Fresh build from a clean checkout (opt-in; needs the `build` frontend).      #
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@pytest.mark.skipif(
    importlib.util.find_spec("build") is None,
    reason="the `build` package is not installed",
)
def test_wheel_builds_from_repo_root(tmp_path):
    proc = _run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(tmp_path),
            str(_REPO_ROOT),
        ]
    )
    assert proc.returncode == 0, proc.stderr
    wheels = list(tmp_path.glob("oolu-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    # Every subpackage must ship, including the persistence migration runner.
    assert any(n.endswith("oolu/cli.py") for n in names)
    assert any(n.endswith("oolu/persistence/migrations.py") for n in names)
