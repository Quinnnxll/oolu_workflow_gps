"""The install-trap fixes from the DX audit: every dead end grows directions.

The audit walked a fresh Windows machine into six consecutive tracebacks:
bare-file cli.py invocation, a pip-less venv, missing engine extras, a
missing API key, and a silent dead `localhost:8000` model target. These
tests pin the remedies: `oolu doctor` diagnoses each trap with its exact
fix, `oolu run` preflights the same traps before any engine machinery can
produce a misleading traceback, the bare-file invocation and the
`uvicorn ...asgi:app` dead ends answer with directions, and the setup
scripts bootstrap pip when a stripped Python omits it.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
from pathlib import Path

from oolu import cli

ROOT = Path(__file__).resolve().parent.parent


def _doctor(monkeypatch, *, modules=(), endpoint_error=None, key="k"):
    """Run doctor with the environment fully scripted."""
    monkeypatch.setattr(cli, "_module_available", lambda name: name in modules)
    monkeypatch.setattr(cli, "_probe_endpoint", lambda base, **_: endpoint_error)
    if key is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    out = io.StringIO()
    code = cli.main(["doctor"], out=out)
    return code, out.getvalue()


def test_doctor_passes_on_a_healthy_engine_machine(monkeypatch):
    code, report = _doctor(
        monkeypatch,
        modules=("langgraph", "litellm", "uvicorn", "playwright", "docker"),
        endpoint_error=None,
    )
    assert code == 0, report
    assert "model engine" in report and "fast tier" in report
    assert "Everything this machine needs is in place." in report


def test_doctor_names_the_engine_extra_when_missing(monkeypatch):
    code, report = _doctor(monkeypatch, modules=("uvicorn",))
    # Missing OPTIONAL stacks are guidance, not failure: the desktop-only
    # user has a healthy machine.
    assert code == 0, report
    assert 'pip install "oolu[engine]"' in report
    assert "needed only for `oolu run`" in report


def test_doctor_fails_loudly_on_a_dead_model_endpoint(monkeypatch):
    code, report = _doctor(
        monkeypatch,
        modules=("langgraph", "litellm", "uvicorn"),
        endpoint_error="connection refused",
    )
    assert code == 1
    assert "localhost:8000" in report and "connection refused" in report
    assert "vLLM / Ollama / LM Studio" in report  # the fix, not a shrug
    assert "problem(s) found" in report


def test_doctor_flags_the_missing_api_key(monkeypatch):
    code, report = _doctor(
        monkeypatch,
        modules=("langgraph", "litellm", "uvicorn"),
        endpoint_error=None,
        key=None,
    )
    assert code == 1
    assert "OPENAI_API_KEY" in report and "OPENAI_API_KEY=EMPTY" in report


# --------------------------------------------------------------------------- #
# `oolu run` preflight: the audit's tracebacks become one-line fixes.          #
# --------------------------------------------------------------------------- #
def test_run_preflight_catches_the_missing_engine(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_module_available", lambda name: False)
    code = cli.main(["run", "do the thing"])
    assert code == 2
    err = capsys.readouterr().err
    assert 'pip install "oolu[engine]"' in err
    assert "Traceback" not in err


def test_run_preflight_catches_the_dead_model_server(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_module_available", lambda name: True)
    monkeypatch.setattr(cli, "_probe_endpoint", lambda base, **_: "refused")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    code = cli.main(["run", "do the thing"])
    assert code == 2
    err = capsys.readouterr().err
    assert "no model server is answering" in err
    assert "localhost:8000" in err and "oolu doctor" in err


def test_run_preflight_catches_the_missing_api_key(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_module_available", lambda name: True)
    monkeypatch.setattr(cli, "_probe_endpoint", lambda base, **_: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = cli.main(["run", "do the thing"])
    assert code == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_an_injected_builder_skips_preflight_and_the_flag_parses(monkeypatch):
    # Embedders and tests bring their own model stack: no probes for them.
    monkeypatch.setattr(
        cli, "_probe_endpoint", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    out = io.StringIO()
    code = cli.main(
        ["run", "hi", "--json"], builder=lambda settings, **kw: _Engine(), out=out
    )
    assert code == 0
    assert cli.build_parser().parse_args(["run", "x", "--no-preflight"]).no_preflight


class _Engine:
    def run(self, intent):
        class _Result:
            success = True

        return _Result()


# --------------------------------------------------------------------------- #
# The two classic dead ends answer with directions now.                        #
# --------------------------------------------------------------------------- #
def test_bare_file_invocation_gives_directions_not_an_import_traceback():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "src" / "oolu" / "cli.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 2
    assert "setup.bat" in proc.stderr and "python -m oolu.cli" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_uvicorn_asgi_app_is_a_signpost_not_an_attribute_error():
    from oolu.gateway.asgi import app  # the exact target the audit hit

    sent = []

    async def drive():
        scope = {"type": "http", "method": "GET", "path": "/"}

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)

    asyncio.run(drive())
    start, body = sent
    assert start["status"] == 503  # honestly not a service, not a fake one
    text = body["body"].decode()
    assert "oolu desktop" in text and "setup.bat" in text
    assert "GatewayASGI(app)" in text  # and the real embedding path


# --------------------------------------------------------------------------- #
# Setup scripts and CI wiring.                                                 #
# --------------------------------------------------------------------------- #
def test_setup_scripts_bootstrap_pip_when_the_venv_lacks_it():
    for name in ("setup.sh", "setup.bat"):
        script = (ROOT / name).read_text()
        assert "ensurepip" in script, name
        assert "-m pip --version" in script, name  # check first, then bootstrap


def test_every_workflow_can_be_dispatched_by_hand():
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert [w.name for w in workflows] == [
        "build-installers.yml",
        "ci.yml",
        "desktop-windows.yml",
    ]
    for workflow in workflows:
        assert "workflow_dispatch" in workflow.read_text(), workflow.name


def test_ci_lints_and_runs_the_suite():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "python -m pytest" in ci
    assert "ruff check src tests" in ci
    assert '".[serve,http,oidc,postgres]"' in ci  # the extras the suite needs


def test_readme_documents_the_doctor():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "oolu doctor" in readme
    assert 'pip install "oolu[engine]"' in readme
