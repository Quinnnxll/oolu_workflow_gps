"""Phase 3 of the context-harness plan: the build seat gets its context
pushed, budgeted, and traced.

The compiler assembles slot vocabulary, upstream shapes, similar
contracts, and verified example functions into one labeled block; the
budget enforces the spec's compaction order (verbatim classes survive,
examples drop first, every drop recorded); both authoring paths carry
the pack ahead of the request; and the synthesis/repair loops now see
their full error ledgers instead of only the latest failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

from oolu.author import NodeAuthorAgent
from oolu.chat import author_node_function
from oolu.contextpack import (
    ContextPackCompiler,
    NodeExample,
    compose_build_request,
    similarity,
)
from oolu.models import ErrorClass, ErrorRecord, GraphState
from oolu.routing.prompting import PromptAssembler

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))


CATALOG = [
    {
        "node_id": "node-fetch-sales",
        "title": "Fetch sales rows",
        "goal": "fetch the day's sales rows from the store ledger",
        "consumes": [],
        "produces": [{"name": "sales_rows", "type": "str"}],
    },
    {
        "node_id": "node-notify",
        "title": "Send a notification",
        "goal": "send a short notification message",
        "consumes": [{"name": "message", "type": "str"}],
        "produces": [{"name": "delivery", "type": "str"}],
    },
]

UPSTREAM = [
    {
        "node_id": "node-fetch-sales",
        "title": "Fetch sales rows",
        "outputs": [
            {
                "run_id": "r-1",
                "status": "ok",
                "outputs": [{"name": "sales_rows", "value": '[{"qty": 3}]'}],
            }
        ],
    }
]


# --------------------------------------------------------------------------- #
# The compiler                                                                 #
# --------------------------------------------------------------------------- #
def test_the_pack_carries_vocabulary_shapes_contracts_and_examples():
    pack = ContextPackCompiler().compile(
        "total the revenue of the sales rows the fetch-sales node produces",
        catalog=CATALOG,
        examples=[
            NodeExample(card=CATALOG[0], script="emit_result({'x': 1})", score=0.9)
        ],
        upstream=UPSTREAM,
        lessons=["read bindings.json, never retype values"],
    )
    assert "sales_rows(str)" in pack.text  # the vocabulary, exact names
    assert '"sales_rows"' in pack.text  # the upstream shape, verbatim
    assert "Fetch sales rows" in pack.text  # the contract line
    assert "```python" in pack.text  # the example function
    assert "never retype values" in pack.text
    assert "slot-vocabulary" in pack.included
    assert "upstream:node-fetch-sales" in pack.included
    assert pack.tokens > 0 and not pack.excluded


def test_an_empty_desk_compiles_an_empty_pack():
    pack = ContextPackCompiler().compile("slugify the title", catalog=[])
    assert pack.empty and pack.tokens == 0


def test_the_budget_drops_examples_first_and_records_every_drop():
    big_script = "x = 1\n" * 400
    examples = [
        NodeExample(card=CATALOG[0], script=big_script, score=0.9),
        NodeExample(card=CATALOG[1], script=big_script, score=0.4),
    ]
    # A window small enough that both example bodies cannot ride.
    pack = ContextPackCompiler(window=4000).compile(
        "total the sales rows", catalog=CATALOG, examples=examples,
        upstream=UPSTREAM,
    )
    # The verbatim classes survived compaction whole...
    assert "sales_rows(str)" in pack.text
    assert '"sales_rows"' in pack.text
    # ...the lowest-scoring example went first, and the drop is on the
    # record — a silently truncated pack reads as complete when it isn't.
    assert any(e.startswith("example:node-notify") for e in pack.excluded)
    assert pack.tokens <= ContextPackCompiler(window=4000).budget_tokens() or (
        not any("example" in i for i in pack.included)
    )


def test_similarity_ranks_the_related_node_first():
    goal = "fetch the sales rows for today"
    scored = sorted(
        CATALOG,
        key=lambda n: similarity(goal, f"{n['title']} {n['goal']}"),
        reverse=True,
    )
    assert scored[0]["node_id"] == "node-fetch-sales"


# --------------------------------------------------------------------------- #
# The request shape both paths send                                            #
# --------------------------------------------------------------------------- #
def test_compose_is_the_bare_goal_when_nothing_rides():
    assert compose_build_request("slugify the title") == "slugify the title"


def test_compose_puts_context_first_and_the_request_last():
    content = compose_build_request(
        "slugify the title", context="=== Desk context ===\nvocab"
    )
    assert content.startswith("=== Desk context ===")
    assert content.rstrip().endswith("REQUEST:\nslugify the title".rstrip())


class _CapturingAuthor:
    def __init__(self):
        self.contents: list[str] = []

    def reply(self, messages):
        self.contents.append(str(messages[-1]["content"]))
        return "NO_TASK"


def test_the_one_shot_author_receives_the_pack():
    author = _CapturingAuthor()
    author_node_function(
        author, "build the thing", context="=== Desk context ===\nvocab"
    )
    assert author.contents[0].startswith("=== Desk context ===")
    assert "REQUEST:\nbuild the thing" in author.contents[0]


def test_the_agent_transcript_carries_the_pack():
    captured: list[list[dict]] = []

    class _Model:
        def consult(self, messages, *, tools):
            captured.append([dict(m) for m in messages])
            raise RuntimeError("stop after capture")

    agent = NodeAuthorAgent(_Model())
    agent.author("build the thing", context="=== Desk context ===\nvocab")
    user_turn = captured[0][-1]
    assert user_turn["role"] == "user"
    assert user_turn["content"].startswith("=== Desk context ===")
    assert "REQUEST:\nbuild the thing" in user_turn["content"]


# --------------------------------------------------------------------------- #
# The bench mirrors the gateway                                                #
# --------------------------------------------------------------------------- #
def test_route_position_goals_see_their_upstream_shape_one_shot():
    from node_authoring import GOALS, bench_context_pack

    goal = next(g for g in GOALS if g.key == "route-sales")
    pack = bench_context_pack(goal)
    assert '"sales_rows"' in pack  # the exact verified shape, pushed
    assert "OL-1" in pack  # down to the real sample values

    plain = next(g for g in GOALS if g.key == "slugify")
    assert "OL-1" not in bench_context_pack(plain)  # no invented upstream


def test_the_incumbent_still_holds_the_line_with_the_pack_riding():
    from node_authoring import fit_for_the_seat, run_bench, scripted_author

    report = run_bench(scripted_author(), name="incumbent")
    assert report.verified_rate == 1.0, report.as_dict()
    assert fit_for_the_seat(report)


# --------------------------------------------------------------------------- #
# The error ledger reaches the model                                           #
# --------------------------------------------------------------------------- #
def _state_with_errors(*messages: str) -> GraphState:
    state = GraphState(intent="convert the file", session_id="pack-test")
    for index, message in enumerate(messages):
        state.error_history.append(
            ErrorRecord.create(
                error_class=ErrorClass.RUNTIME_EXCEPTION,
                message=message,
                iteration=index,
            )
        )
        state.iteration = index + 1
    return state


def test_the_synthesis_prompt_carries_distinct_earlier_failures():
    state = _state_with_errors(
        "KeyError: 'amount'",
        "ValueError: bad date",
        "TypeError: not a list",
    )
    prompt = PromptAssembler().build(state)
    action = prompt.messages[-1]["content"]
    # The latest failure keeps its place...
    assert "TypeError: not a list" in action
    # ...and the DISTINCT earlier ones ride too, labeled as history.
    assert "KeyError: 'amount'" in action
    assert "ValueError: bad date" in action
    assert "DIFFERENT ways" in action


def test_the_ledger_stays_out_of_the_cacheable_prefix():
    state = _state_with_errors("KeyError: 'amount'", "ValueError: bad date")
    assembler = PromptAssembler()
    with_errors = assembler.build(state)
    clean = assembler.build(
        GraphState(intent="convert the file", session_id="other-session")
    )
    # Same frozen prefix bytes, error ledger or none — trap #4 stands.
    assert with_errors.prefix_fingerprint == clean.prefix_fingerprint


def test_the_repair_ledger_rides_inside_the_failure_words(tmp_path):
    """The runner's second repair round tells the model what the first
    already tried — carried inside the error TEXT, so any synthesizer
    with the 3-argument ``repair`` signature keeps working unchanged."""
    from oolu.cache import LocalScriptCache
    from oolu.models import ExecutionResult, Phase
    from oolu.runtime import NodeScriptRunner, StubBackend
    from oolu.skills.models import ActionEvent

    def _boom(request):
        return ExecutionResult(
            phase=Phase.EXECUTE,
            exit_code=1,
            stderr="TypeError: broken by environment drift",
            contract_ok=False,
        )

    seen: list[str] = []

    class _StubbornSynth:
        """Repairs twice, differently each time, never successfully."""

        def synthesize(self, goal, *, session_id):
            return None

        def repair(self, goal, script, error):
            seen.append(error)
            return (
                "from _oolu_runtime import emit_result\n"
                f"emit_result('round {len(seen)}')\n"
            )

    runner = NodeScriptRunner(
        StubBackend([_boom, _boom, _boom]),
        LocalScriptCache(tmp_path / "scripts.db"),
        synthesizer=_StubbornSynth(),
    )
    action = ActionEvent(
        correlation_id="ledger-test",
        adapter="script",
        operation="run",
        parameters={
            "goal": "normalize the file",
            "script": "from _oolu_runtime import emit_result\nemit_result(x)\n",
            "node_key": "node-ledger-test",
        },
    )
    runner.execute(action, idempotency_key="ledger-test")

    assert len(seen) == 2
    # Round one sees only its own failure; round two also sees round
    # one's, labeled as history.
    assert "Earlier repair attempts" not in seen[0]
    assert "Earlier repair attempts this run failed with" in seen[1]
