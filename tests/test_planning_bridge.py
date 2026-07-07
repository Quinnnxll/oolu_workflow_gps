"""Milestone A's key, bridged into planning: intake and route choice.

The same scripted-provider discipline as the chat-router tests: the fake
Anthropic answers with real wire shapes, so the whole chain — keyring ->
router -> intake bridge -> brief parsing -> stated-value binding -> semantic
route pick -> execution — runs the production code path offline. The floor
is tested just as hard: no key, dead provider, reached cap, or a garbage
answer must land exactly where the deterministic paths always landed.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from test_chat_model_router import FakeTransport, _anthropic_reply

from oolu.assembly import build_http_executor, build_orchestrator_factory
from oolu.billing import ModelCallMeter
from oolu.durable.connection import DurableConnection
from oolu.durable.service import DurableWorkflowService
from oolu.orchestrator.adapters import ModelRouteOptimizer
from oolu.orchestrator.intake import ModelBackedIntaker
from oolu.orchestrator.state import TaskContract
from oolu.providers.base import ProviderResponse
from oolu.providers.chatmodel import ChatModelRouter, RouterIntakeModel
from oolu.providers.keyring import ModelKeyring

URL = "https://api.example/report.json"

INTAKE_JSON = json.dumps(
    {
        "parameters": [
            {
                "name": "url",
                "description": "the API endpoint",
                "value_type": "string",
                "required": True,
                "suggested_values": [URL],
                "question": "Which endpoint should I fetch?",
            },
            {
                "name": "format",
                "description": "output format",
                "value_type": "string",
                "required": False,
                "suggested_values": ["csv"],
                "question": "Any preferred format?",
            },
        ],
        "constraints": [],
    }
)


def _rig(tmp_path, transport, purpose="plan.intake", meter=None):
    conn = DurableConnection(tmp_path / "keys.db")
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
    keyring.store("local", "anthropic", "sk-ant-0123456789")
    router = ChatModelRouter(
        keyring, "local", transport=transport, meter=meter, purpose=purpose
    )
    return router, conn


# --------------------------------------------------------------------------- #
# The intake bridge.                                                           #
# --------------------------------------------------------------------------- #
def test_intake_uses_the_key_and_binds_only_what_was_stated(tmp_path):
    transport = FakeTransport()
    transport.script("anthropic.com", 200, _anthropic_reply(INTAKE_JSON))
    router, conn = _rig(tmp_path, transport)
    try:
        brief = ModelBackedIntaker(RouterIntakeModel(router)).intake(
            TaskContract(intent=f"grab the quarterly numbers from {URL}")
        )
        by_name = {p.name: p for p in brief.parameters}
        # The URL was literally in the request: bound, source USER.
        assert by_name["url"].value == URL
        assert by_name["url"].source.value == "user"
        # "csv" was the model's idea, not the user's: stays a question.
        assert by_name["format"].value is None
        assert by_name["format"].question
    finally:
        conn.close()


def test_no_key_degrades_silently_to_the_heuristic_floor(tmp_path):
    conn = DurableConnection(tmp_path / "keys.db")
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")  # empty
    router = ChatModelRouter(keyring, "local", transport=FakeTransport())
    try:
        assert RouterIntakeModel(router).propose("anything") == ""
        brief = ModelBackedIntaker(RouterIntakeModel(router)).intake(
            TaskContract(intent=f"fetch json from {URL}")
        )
        # The heuristic floor still carries the stated URL.
        (param,) = brief.parameters
        assert param.name == "url" and param.value == URL
    finally:
        conn.close()


def test_a_reached_cap_degrades_planning_and_says_nothing(tmp_path):
    from oolu.providers.chatmodel import _Telemetry

    meter = ModelCallMeter()
    meter.record(
        "chat.turn",
        _Telemetry(model="m", tier="fast", prompt_tokens=10_000_000,
                   completion_tokens=10_000_000, duration_s=1.0),
    )
    transport = FakeTransport()
    transport.script("anthropic.com", 200, _anthropic_reply(INTAKE_JSON))
    conn = DurableConnection(tmp_path / "keys.db")
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
    keyring.store("local", "anthropic", "sk-ant-0123456789")
    router = ChatModelRouter(
        keyring, "local", transport=transport, meter=meter,
        budget=lambda: 0.01, purpose="plan.intake",
    )
    try:
        # The cap is shared across purposes: chat spend blocks planning too,
        # and planning degrades to "" instead of raising into the run.
        assert RouterIntakeModel(router).propose("anything") == ""
        assert transport.requests == []
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Semantic route choice.                                                       #
# --------------------------------------------------------------------------- #
class _Menu:
    """A scripted route-picker: replies with whatever the test says."""

    def __init__(self, answer):
        self._answer = answer
        self.prompts = []

    def reply(self, messages):
        self.prompts.append(messages[-1]["content"])
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def _plan(model):
    from test_http_gateway import _blueprint

    from oolu.orchestrator.adapters import LeastCostRouteOptimizer
    from oolu.orchestrator.state import SemanticGrounding
    from oolu.skills.requirements import (
        AuthorizationGrant,
        AuthorizationMode,
        RequirementBrief,
    )

    blueprints = [
        _blueprint(operation="get", capability="get").model_copy(
            update={"name": "Fetch JSON from an API"}
        ),
        _blueprint(operation="run", capability="run").model_copy(
            update={"name": "Interact with a Dynamic Dropdown"}
        ),
    ]
    optimizer = ModelRouteOptimizer(
        LeastCostRouteOptimizer(blueprints), model=model
    )
    brief = RequirementBrief(
        intent="open the dropdown on the page",
        parameters=[],
        constraints=[],
        authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
    )
    grounding = SemanticGrounding(
        edges=[], resolved_capabilities=frozenset({"get", "run"}),
        unresolved_terms=[],
    )
    return optimizer.optimize(brief, grounding)


def test_the_model_reorders_viable_routes():
    model = _Menu("2")
    plan = _plan(model)
    assert plan.chosen.name == "Interact with a Dynamic Dropdown"
    # The menu the model saw carried both viable routes.
    assert "1. Fetch JSON from an API" in model.prompts[0]
    assert "2. Interact with a Dynamic Dropdown" in model.prompts[0]


@pytest.mark.parametrize("answer", ["banana", "0", "99", ""])
def test_unusable_answers_keep_the_deterministic_choice(answer):
    plan = _plan(_Menu(answer))
    assert plan.chosen.name == "Fetch JSON from an API"  # least-cost order


def test_a_dead_model_keeps_the_deterministic_choice():
    from oolu.chat import ModelUnavailable

    plan = _plan(_Menu(ModelUnavailable("no key")))
    assert plan.chosen.name == "Fetch JSON from an API"


def test_no_model_means_no_consultation():
    plan = _plan(None)
    assert plan.chosen.name == "Fetch JSON from an API"


# --------------------------------------------------------------------------- #
# The whole brain-to-hands chain, end to end.                                  #
# --------------------------------------------------------------------------- #
class BrainTransport:
    """One fake Anthropic playing both planning roles, told apart by the
    system prompt each consultation carries."""

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        system = (body or {}).get("system", "")
        if "structured brief" in system:
            return ProviderResponse(200, _anthropic_reply(INTAKE_JSON))
        if "Pick the route" in system:
            menu = body["messages"][-1]["content"]
            match = re.search(r"(\d+)\. Fetch JSON from an API", menu)
            return ProviderResponse(200, _anthropic_reply(match.group(1)))
        return ProviderResponse(500, {"error": "unexpected consultation"})


def test_the_key_plans_and_the_hands_execute(tmp_path):
    from oolu.skills.pack import load_starter_pack
    from oolu.skills.registry import SkillRegistry

    registry = SkillRegistry(tmp_path / "skills.db")
    load_starter_pack(registry)
    skills = [entry.skill for entry in registry.list()]
    registry.close()

    meter = ModelCallMeter()
    transport = BrainTransport()
    intake_router, conn = _rig(tmp_path, transport, "plan.intake", meter)
    keyring = intake_router._keyring
    route_router = ChatModelRouter(
        keyring, "local", transport=transport, meter=meter, purpose="plan.route"
    )

    executors = build_http_executor()
    executors["http"]._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"total": 48200})
        )
    )
    executors["http"]._resolver = lambda host: ["93.184.216.34"]

    factory = build_orchestrator_factory(
        skills=skills,
        executors=executors,
        intake_model=RouterIntakeModel(intake_router),
        route_model=route_router,
    )
    durable_conn = DurableConnection(tmp_path / "durable.db")
    durable = DurableWorkflowService(durable_conn, factory)
    try:
        state = durable.submit(
            TaskContract(intent=f"grab the quarterly numbers from {URL}")
        )
        assert state.phase.value == "completed", state.failure_reason
        assert state.route.chosen.name == "Fetch JSON from an API"
        (output,) = state.result["outputs"]
        assert '"total"' in output["body"]
        # Both consultations entered the books under their own purposes.
        assert meter.charges("plan.intake")
        assert meter.charges("plan.route")
    finally:
        durable_conn.close()
        conn.close()
