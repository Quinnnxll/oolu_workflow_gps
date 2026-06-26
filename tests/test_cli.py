"""Unit tests for the CLI, using an injected fake builder (no vLLM/litellm needed)."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace as NS

import pytest

from workflow_gps.cli import main
from workflow_gps.knowledge import LocalKnowledgeClient

OK = NS(success=True, status=NS(value="completed"), answer={"slug": "hi"}, failure_reason=None,
        recalc_count=1, tier_escalations=0, final_tier=NS(value="fast"), attempts=2)
FAIL = NS(success=False, status=NS(value="failed"), answer=None, failure_reason="exhausted",
          recalc_count=6, tier_escalations=1, final_tier=NS(value="reasoning"), attempts=7)


def _builder(result):
    cap: dict = {}

    def build(settings, knowledge=None):
        cap["settings"] = settings
        cap["knowledge"] = knowledge

        class Eng:
            def run(self, intent):
                cap["intent"] = intent
                return result

        return Eng()

    return build, cap


def test_run_success_exit_zero():
    b, cap = _builder(OK)
    assert main(["run", "convert csv"], builder=b, out=io.StringIO()) == 0
    assert cap["intent"] == "convert csv" and cap["knowledge"] is None


def test_run_failure_exit_one():
    b, _ = _builder(FAIL)
    assert main(["run", "x", "--json"], builder=b, out=io.StringIO()) == 1


def test_json_output():
    b, _ = _builder(OK)
    buf = io.StringIO()
    assert main(["run", "x", "--json"], builder=b, out=buf) == 0
    payload = json.loads(buf.getvalue())
    assert payload["success"] and payload["answer"] == {"slug": "hi"} and payload["final_tier"] == "fast"


def test_knowledge_local_injected():
    b, cap = _builder(OK)
    main(["run", "x", "--knowledge", "local", "--knowledge-db", ":memory:"], builder=b, out=io.StringIO())
    assert isinstance(cap["knowledge"], LocalKnowledgeClient)


def test_backend_override():
    b, cap = _builder(OK)
    main(["run", "x", "--backend", "docker"], builder=b, out=io.StringIO())
    assert cap["settings"].backend.kind == "docker"


def test_remote_without_env_is_config_error(monkeypatch):
    monkeypatch.delenv("WFGPS_KNOWLEDGE_URL", raising=False)
    monkeypatch.delenv("WFGPS_KNOWLEDGE_TOKEN", raising=False)
    b, _ = _builder(OK)
    assert main(["run", "x", "--knowledge", "remote"], builder=b, out=io.StringIO()) == 2


def test_show_config():
    buf = io.StringIO()
    assert main(["show-config"], out=buf) == 0
    assert "fast tier" in buf.getvalue() and "Qwen" in buf.getvalue()


def test_version():
    buf = io.StringIO()
    assert main(["version"], out=buf) == 0 and "workflow-gps" in buf.getvalue()


def test_missing_subcommand_exits_two():
    with pytest.raises(SystemExit) as exc:
        main([], out=io.StringIO())
    assert exc.value.code == 2
