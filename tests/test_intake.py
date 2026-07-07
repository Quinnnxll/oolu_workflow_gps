"""Model-backed natural-language intake tests.

Exit gate: intake turns a free-text intent into a structured brief *without*
crossing the system's safety lines — it never binds a value (only suggests),
never self-authorizes (a brief from intake is always GUIDED), and never lets a
bad or absent model kill the run (it degrades to a deterministic brief). The
compiler then consumes the brief exactly as it would a hand-built one.
"""

from __future__ import annotations

from oolu.orchestrator import (
    HeuristicIntaker,
    ModelBackedIntaker,
    StaticIntaker,
)
from oolu.orchestrator.state import TaskContract
from oolu.skills.requirements import (
    AuthorizationMode,
    ParameterSource,
    RequirementConstraintCompiler,
)


class _FakeModel:
    """A scripted IntakeModel — a completion string, or an Exception to raise."""

    def __init__(self, answer):
        self._answer = answer

    def propose(self, intent: str) -> str:
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def _contract(intent="book a flight to Lisbon"):
    return TaskContract(intent=intent, submitted_by="local-user")


def test_heuristic_intaker_invents_nothing():
    brief = HeuristicIntaker().intake(_contract())
    assert brief.intent == "book a flight to Lisbon"
    assert brief.parameters == []
    assert brief.authorization.mode is AuthorizationMode.GUIDED


def test_model_brief_produces_clarifying_questions():
    answer = """Here is the brief:
```json
{"parameters": [
  {"name": "destination", "description": "where to fly", "value_type": "string",
   "required": true, "suggested_values": ["Lisbon"], "question": "Which city?",
   "question_priority": 5},
  {"name": "cabin", "description": "seat class", "value_type": "string",
   "required": true, "options": ["economy", "business"], "question": "Which cabin?"}
]}
```"""
    brief = ModelBackedIntaker(_FakeModel(answer)).intake(_contract())
    assert [p.name for p in brief.parameters] == ["destination", "cabin"]
    # "Lisbon" is verbatim in the user's own words, so it binds (source
    # USER) and the user is not asked to repeat themselves...
    destination = brief.parameters[0]
    assert destination.value == "Lisbon"
    assert destination.source is ParameterSource.USER
    result = RequirementConstraintCompiler().compile(brief)
    # ...while the cabin — which the user never mentioned — is still asked.
    assert result.status.value == "clarifying"
    assert [q.parameter for q in result.questions] == ["cabin"]


def test_model_may_suggest_but_never_binds_a_value():
    # The model tries to pin "Paris" itself; intake demotes it. What binds
    # instead is "Lisbon" — because the USER typed it in the intent, and
    # code (verbatim containment), not the model, verifies that. A
    # suggestion absent from the user's words stays an open question.
    answer = (
        '{"parameters": [{"name": "destination", "value_type": "string", '
        '"value": "Paris", "source": "user", "suggested_values": ["Lisbon"]}, '
        '{"name": "hotel", "value_type": "string", '
        '"suggested_values": ["Ritz"], "question": "Which hotel?"}]}'
    )
    brief = ModelBackedIntaker(_FakeModel(answer)).intake(_contract())
    destination, hotel = brief.parameters
    assert destination.value == "Lisbon"  # the user's word, never Paris
    assert destination.source is ParameterSource.USER
    assert destination.suggested_values == ["Lisbon"]
    assert hotel.value is None  # the model's idea stays a question
    assert hotel.source is ParameterSource.UNRESOLVED


def test_model_cannot_self_authorize():
    answer = (
        '{"authorization": "fully_delegated", "parameters": '
        '[{"name": "x", "value_type": "string"}]}'
    )
    brief = ModelBackedIntaker(_FakeModel(answer)).intake(_contract())
    assert brief.authorization.mode is AuthorizationMode.GUIDED


def test_malformed_parameters_are_dropped_not_fatal():
    answer = (
        '{"parameters": [42, {"description": "no name"}, '
        '{"name": "good", "value_type": "string"}]}'
    )
    brief = ModelBackedIntaker(_FakeModel(answer)).intake(_contract())
    assert [p.name for p in brief.parameters] == ["good"]


def test_unusable_answer_falls_back_to_heuristic():
    for answer in ("no json here at all", "```json\n{not valid}\n```", ""):
        brief = ModelBackedIntaker(_FakeModel(answer)).intake(_contract())
        assert brief.intent == "book a flight to Lisbon"
        assert brief.parameters == []
        assert brief.authorization.mode is AuthorizationMode.GUIDED


def test_model_transport_failure_degrades_without_raising():
    brief = ModelBackedIntaker(_FakeModel(RuntimeError("endpoint down"))).intake(
        _contract()
    )
    assert brief.parameters == []
    assert brief.intent == "book a flight to Lisbon"


def test_no_model_uses_deterministic_fallback():
    brief = ModelBackedIntaker(model=None).intake(_contract("summarize my inbox"))
    assert brief.intent == "summarize my inbox"
    assert brief.parameters == []


def test_intaker_satisfies_the_orchestrator_port():
    # Both the new adapter and the legacy static one honor the same Intaker port.
    from oolu.orchestrator import Intaker

    assert isinstance(ModelBackedIntaker(), Intaker)
    assert isinstance(HeuristicIntaker(), Intaker)
    static = StaticIntaker(HeuristicIntaker().intake(_contract()))
    assert isinstance(static, Intaker)
