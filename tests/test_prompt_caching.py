"""Unit tests for cache-safe prompt assembly (goal 3).

The load-bearing property: the cacheable prefix is byte-identical across recalc
cycles that differ only in volatile state.
"""

from __future__ import annotations

from workflow_gps.models import ErrorClass, ErrorRecord, ExecutionPlan, GraphState
from workflow_gps.routing.prompting import DEFAULT_SYSTEM_PROMPT, PromptAssembler

INTENT = "convert sales.csv to a bar chart"


def _err(msg, i=0):
    return ErrorRecord.create(
        error_class=ErrorClass.SYNTAX_ERROR,
        message=msg,
        exception_type="SyntaxError",
        iteration=i,
    )


def test_structure_is_system_task_action():
    p = PromptAssembler().build(GraphState(intent=INTENT, session_id="s"))
    assert [m["role"] for m in p.messages] == ["system", "user", "user"]
    assert p.prefix_len == 2


def test_prefix_invariant_across_volatile_state():
    asm = PromptAssembler()
    fresh = asm.build(GraphState(intent=INTENT, session_id="A", iteration=0))
    erred = asm.build(
        GraphState(
            intent=INTENT, session_id="B", iteration=2, error_history=[_err("eof")]
        )
    )
    rut = asm.build(
        GraphState(
            intent=INTENT,
            session_id="C",
            iteration=3,
            error_history=[_err("eof", i) for i in range(3)],
            plan=ExecutionPlan(intent=INTENT, required_dependencies=["pandas"]),
        )
    )
    assert (
        fresh.cacheable_messages == erred.cacheable_messages == rut.cacheable_messages
    )
    assert (
        fresh.prefix_fingerprint == erred.prefix_fingerprint == rut.prefix_fingerprint
    )


def test_system_is_the_frozen_contract():
    p = PromptAssembler().build(GraphState(intent=INTENT, session_id="s"))
    assert p.messages[0]["content"] == DEFAULT_SYSTEM_PROMPT
    assert (
        "emit_result" in p.messages[0]["content"]
        and "```python" in p.messages[0]["content"]
    )


def test_volatile_state_only_in_action_message():
    asm = PromptAssembler(verify_cache_safety=True)
    rut = asm.build(
        GraphState(
            intent=INTENT,
            session_id="sess-X",
            iteration=3,
            error_history=[_err("unexpected EOF", i) for i in range(3)],
        )
    )
    prefix = rut.messages[0]["content"] + rut.messages[1]["content"]
    assert "sess-X" not in prefix and "unexpected EOF" not in prefix
    action = rut.volatile_messages[0]["content"]
    assert "occurred 3 times" in action and "Do NOT" in action


def test_rut_announces_installed_deps():
    asm = PromptAssembler()
    p = asm.build(
        GraphState(
            intent=INTENT,
            session_id="s",
            iteration=1,
            error_history=[_err("x")],
            plan=ExecutionPlan(
                intent=INTENT, required_dependencies=["pandas", "matplotlib"]
            ),
        )
    )
    assert "pandas, matplotlib" in p.volatile_messages[0]["content"]
