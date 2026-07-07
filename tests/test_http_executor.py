"""The engine's hands: the HTTP executor, its guard, and the full arm.

The guard tests use a stubbed resolver so refusal never depends on the
test network; the wire tests use ``httpx.MockTransport`` so the request
pipeline (streaming, redirects, size caps) is the real httpx code path.
The end-to-end test runs the actual starter pack through the actual
orchestrator: intent in, clarified URL bound, fetched body on the result.
"""

from __future__ import annotations

import httpx

from oolu.assembly import build_http_executor, build_orchestrator_factory
from oolu.durable.connection import DurableConnection
from oolu.durable.service import DurableWorkflowService
from oolu.orchestrator.state import TaskContract
from oolu.skills.http_adapter import HttpActionExecutor, HttpExecutionPolicy
from oolu.skills.models import ActionEvent, ExecutionStatus

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - a resolver stub


def _action(url=None, **params):
    if url is not None:
        params["url"] = url
    return ActionEvent(correlation_id="c1", adapter="http", operation="get", parameters=params)


def _executor(handler, policy=None, resolver=PUBLIC):
    return HttpActionExecutor(
        policy or HttpExecutionPolicy(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=resolver,
    )


def _ok_json(request):
    return httpx.Response(200, json={"revenue": 42}, headers={"content-type": "application/json"})


def test_a_public_get_succeeds_with_the_body_as_evidence():
    executor = _executor(_ok_json)
    outcome = executor.execute(_action("https://api.example/report"), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["status"] == 200
    assert '"revenue"' in outcome.evidence["body"]
    assert outcome.evidence["truncated"] is False


def test_private_loopback_and_metadata_addresses_are_refused():
    calls = []

    def never(request):
        calls.append(request)
        return httpx.Response(200)

    for resolved in (["127.0.0.1"], ["10.0.0.7"], ["169.254.169.254"], ["192.168.1.1"]):
        executor = _executor(never, resolver=lambda host, r=resolved: r)
        outcome = executor.execute(
            _action("http://internal.example/secrets"), idempotency_key=f"k-{resolved[0]}"
        )
        assert outcome.status is ExecutionStatus.BLOCKED
        assert "public address" in outcome.error
    assert calls == []  # the guard fires before any request leaves


def test_the_allowlist_narrows_beyond_the_guard():
    policy = HttpExecutionPolicy(allow_hosts=frozenset({"api.example"}))
    executor = _executor(_ok_json, policy=policy)
    ok = executor.execute(_action("https://api.example/x"), idempotency_key="k1")
    sub = executor.execute(_action("https://v2.api.example/x"), idempotency_key="k2")
    other = executor.execute(_action("https://elsewhere.example/x"), idempotency_key="k3")
    assert ok.status is ExecutionStatus.SUCCEEDED
    assert sub.status is ExecutionStatus.SUCCEEDED
    assert other.status is ExecutionStatus.BLOCKED
    assert "allowlist" in other.error


def test_a_redirect_to_a_private_address_dies_at_the_bounce():
    def bouncing(request):
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/meta"})
        return httpx.Response(200, text="metadata")

    def resolver(host):
        return ["169.254.169.254"] if host == "169.254.169.254" else ["93.184.216.34"]

    executor = _executor(bouncing, resolver=resolver)
    outcome = executor.execute(_action("https://public.example/x"), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.BLOCKED
    assert "public address" in outcome.error


def test_reads_are_bounded_and_marked_truncated():
    def huge(request):
        return httpx.Response(200, text="x" * 100_000)

    policy = HttpExecutionPolicy(max_bytes=10_000, evidence_bytes=100)
    executor = _executor(huge, policy=policy)
    outcome = executor.execute(_action("https://api.example/big"), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert len(outcome.evidence["body"]) == 100
    assert outcome.evidence["truncated"] is True


def test_non_2xx_fails_with_the_status_spoken():
    executor = _executor(lambda request: httpx.Response(503, text="down"))
    outcome = executor.execute(_action("https://api.example/x"), idempotency_key="k1")
    assert outcome.status is ExecutionStatus.FAILED
    assert "503" in outcome.error


def test_junk_is_refused_before_the_network():
    executor = _executor(_ok_json)
    no_url = executor.execute(_action(), idempotency_key="k1")
    assert no_url.status is ExecutionStatus.FAILED
    assert "no url" in no_url.error
    ftp = executor.execute(_action("ftp://files.example/x"), idempotency_key="k2")
    assert ftp.status is ExecutionStatus.BLOCKED


def test_replays_are_idempotent():
    hits = []

    def counting(request):
        hits.append(request)
        return httpx.Response(200, text="once")

    executor = _executor(counting)
    first = executor.execute(_action("https://api.example/x"), idempotency_key="same")
    again = executor.execute(_action("https://api.example/x"), idempotency_key="same")
    assert first.id == again.id
    assert len(hits) == 1


# --------------------------------------------------------------------------- #
# The wrist: brief values flow into actions.                                   #
# --------------------------------------------------------------------------- #
def test_bind_brief_parameters_fills_and_substitutes():
    from test_http_gateway import _blueprint

    from oolu.orchestrator.adapters import bind_brief_parameters
    from oolu.orchestrator.state import RoutePlan
    from oolu.skills.requirements import (
        ParameterDomain,
        ParameterSource,
        RequirementBrief,
        RequirementParameter,
    )

    blueprint = _blueprint(operation="get", capability="get")
    action = blueprint.actions[0].action.model_copy(
        update={"parameters": {"fixed": "stays", "templated": "see {{url}}"}}
    )
    blueprint = blueprint.model_copy(
        update={"actions": [blueprint.actions[0].model_copy(update={"action": action})]}
    )
    route = RoutePlan(chosen=blueprint, alternatives=[])
    brief = RequirementBrief(
        intent="fetch",
        parameters=[
            RequirementParameter(
                name="url",
                description="",
                domain=ParameterDomain(value_type="string"),
                value="https://api.example/r",
                source=ParameterSource.USER,
            ),
            RequirementParameter(
                name="fixed",
                description="",
                domain=ParameterDomain(value_type="string"),
                value="must-not-win",
                source=ParameterSource.USER,
            ),
        ],
    )

    bound = bind_brief_parameters(route, brief)
    params = bound.chosen.actions[0].action.parameters
    assert params["url"] == "https://api.example/r"          # filled in
    assert params["templated"] == "see https://api.example/r"  # substituted
    assert params["fixed"] == "stays"                          # skill wins
    # The original route object is untouched (the record stays as planned).
    assert "url" not in route.chosen.actions[0].action.parameters


def test_heuristic_intake_carries_a_stated_url():
    from oolu.orchestrator.intake import HeuristicIntaker

    brief = HeuristicIntaker().intake(
        TaskContract(intent="fetch json from https://api.example/report.json please")
    )
    (param,) = brief.parameters
    assert param.name == "url"
    assert param.value == "https://api.example/report.json"

    empty = HeuristicIntaker().intake(TaskContract(intent="convert the report"))
    assert empty.parameters == []


# --------------------------------------------------------------------------- #
# The whole arm: starter pack + executor + orchestrator, end to end.            #
# --------------------------------------------------------------------------- #
def test_a_stated_url_is_fetched_end_to_end(tmp_path):
    from oolu.skills.pack import load_starter_pack
    from oolu.skills.registry import SkillRegistry

    registry = SkillRegistry(tmp_path / "skills.db")
    load_starter_pack(registry)
    skills = [entry.skill for entry in registry.list()]
    registry.close()

    executors = build_http_executor()
    http = executors["http"]
    http._client = httpx.Client(transport=httpx.MockTransport(_ok_json))
    http._resolver = PUBLIC

    factory = build_orchestrator_factory(skills=skills, executors=executors)
    conn = DurableConnection(tmp_path / "durable.db")
    durable = DurableWorkflowService(conn, factory)
    try:
        state = durable.submit(
            TaskContract(intent="fetch json from https://api.example/report.json")
        )
        assert state.phase.value == "completed", state.failure_reason
        assert state.result["status"] == "succeeded"
        (output,) = state.result["outputs"]
        assert output["status"] == 200
        assert '"revenue"' in output["body"]
    finally:
        conn.close()


def test_without_a_url_the_run_fails_in_words_not_capabilities(tmp_path):
    from oolu.skills.pack import load_starter_pack
    from oolu.skills.registry import SkillRegistry

    registry = SkillRegistry(tmp_path / "skills.db")
    load_starter_pack(registry)
    skills = [entry.skill for entry in registry.list()]
    registry.close()

    executors = build_http_executor()
    executors["http"]._client = httpx.Client(transport=httpx.MockTransport(_ok_json))
    executors["http"]._resolver = PUBLIC

    factory = build_orchestrator_factory(skills=skills, executors=executors)
    conn = DurableConnection(tmp_path / "durable.db")
    durable = DurableWorkflowService(conn, factory)
    try:
        state = durable.submit(TaskContract(intent="convert the report"))
        # No PreflightError 500s, no capability jargon: the run lands as a
        # failure or an actionable pause (here: an incident with retry/
        # abort), never a crash.
        assert state.phase.value == "failed" or state.pause is not None
        for words in (state.failure_reason, state.pause.prompt if state.pause else ""):
            assert "capabilit" not in (words or "")
    finally:
        conn.close()
