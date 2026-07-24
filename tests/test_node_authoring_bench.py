"""The node-authoring bench, pinned as tests — Phase 0's exit gates.

The scoreboard the context-harness plan measures every later phase
against must itself be trustworthy: the scripted incumbent passes the
FIT gate end to end (real subprocess execution, real runtime-contract
parsing, the refusing web broker); the classic failure shapes land in
their taxonomy buckets; the refusal-sentence mapping is pinned to the
repo's own wording; and the new effort telemetry (finish_reason,
context_chars) books through the meter without disturbing older
telemetry shapes.

The heavier, printing variant lives in benchmarks/node_authoring.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from node_authoring import (  # noqa: E402
    BENCH_PURPOSE,
    GOALS,
    BenchScriptRunner,
    _classify_refusal,
    fit_for_the_seat,
    run_bench,
    score_goal,
    scripted_author,
)

from oolu.billing import ModelCallMeter  # noqa: E402


def _goal(key: str):
    return next(g for g in GOALS if g.key == key)


# --------------------------------------------------------------------------- #
# The incumbent holds the line                                                 #
# --------------------------------------------------------------------------- #
def test_the_scripted_incumbent_is_fit_for_the_seat():
    report = run_bench(scripted_author(), name="incumbent")
    assert report.verified_rate == 1.0, report.as_dict()
    assert report.answer_rate == 1.0
    assert report.interface_rate == 1.0
    assert report.first_pass_rate == 1.0
    assert report.truncations == 0
    assert report.conversations_declined
    assert fit_for_the_seat(report)
    assert set(report.taxonomy()) == {"ok"}


def test_conversation_goals_are_declined_not_built():
    report = run_bench(
        scripted_author(),
        goals=tuple(g for g in GOALS if g.kind == "conversation"),
    )
    assert all(r.ok and not r.built for r in report.results)


# --------------------------------------------------------------------------- #
# The classic failure shapes land in their buckets                             #
# --------------------------------------------------------------------------- #
class _Pretender:
    """Authors a function that only pretends — the mock_smells shape."""

    def reply(self, messages):
        return (
            "1. Pretend.\n"
            'IO: {"inputs": [{"name": "title", "type": "str"}], '
            '"outputs": [{"name": "slug", "type": "str"}]}\n'
            "```python\n"
            "from _oolu_runtime import emit_result\n"
            "emit_result('done: 42 rows')\n"
            "```\n"
        )


def test_a_pretending_function_is_classified_mocked():
    result = score_goal(_Pretender(), _goal("slugify"), BenchScriptRunner())
    assert result.failure_class == "mocked"
    assert not result.built


class _Overeager:
    """Builds a node for plain conversation — the judgement failure."""

    def reply(self, messages):
        return (
            "1. Read.\n"
            'IO: {"inputs": [], "outputs": [{"name": "result", "type": "str"}]}\n'
            "```python\n"
            "from _oolu_runtime import emit_result\n"
            "import os\n"
            "emit_result({'result': str(len(os.listdir('.')))})\n"
            "```\n"
        )


def test_building_for_conversation_fails_the_judgement_gate():
    goal = _goal("chat-joke")
    result = score_goal(_Overeager(), goal, BenchScriptRunner())
    assert result.failure_class == "built_conversation"
    report = run_bench(_Overeager(), goals=(goal,))
    assert not report.conversations_declined
    assert not fit_for_the_seat(report)


class _NoInterface:
    """A working script whose IO: line is missing — the silent
    degradation chat.parse_node_io papers over in production."""

    def reply(self, messages):
        return (
            "1. Slugify.\n"
            "```python\n"
            "import json, re\n"
            "from _oolu_runtime import emit_result\n"
            "with open('bindings.json', encoding='utf-8') as fh:\n"
            "    bindings = json.load(fh)\n"
            "slug = re.sub(r'[^a-z0-9]+', '-', bindings['title'].lower()).strip('-')\n"
            "emit_result({'slug': slug})\n"
            "```\n"
        )


def test_a_missing_io_declaration_is_bad_interface_not_silence():
    result = score_goal(_NoInterface(), _goal("slugify"), BenchScriptRunner())
    assert result.verified, result.detail  # the script itself runs fine
    assert result.failure_class == "bad_interface"
    assert result.interface_ok is False


@dataclass
class _Telemetry:
    model: str = "starved"
    tier: str = "fast"
    prompt_tokens: int = 900
    completion_tokens: int = 1024
    duration_s: float = 1.0
    finish_reason: str = "length"
    context_chars: int = 3600


class _Starved:
    """Simulates the 1024-token ceiling: books a "length" finish on the
    meter (as the router now does) and returns a reply cut mid-fence."""

    def __init__(self, meter):
        self._meter = meter

    def reply(self, messages):
        self._meter.record(BENCH_PURPOSE, _Telemetry())
        whole = scripted_author().reply(messages)
        return whole[: int(len(whole) * 0.45)]


def test_a_length_finish_is_classified_truncated():
    meter = ModelCallMeter()
    result = score_goal(
        _Starved(meter),
        _goal("merge-prefs"),
        BenchScriptRunner(),
        meter=meter,
    )
    assert result.truncated
    assert result.failure_class == "truncated"
    assert not result.ok


def test_refusal_sentences_map_to_their_buckets():
    # The repo's own wordings (chat.py / author.py) — pinned so a
    # rewording breaks THIS test instead of silently misclassifying.
    assert _classify_refusal("the model wrote a function that only pretends — x") == "mocked"
    assert _classify_refusal("the model wrote no usable function, so nothing was built") == "no_script"
    assert _classify_refusal("that reads as conversation, not an executable task") == "refused"
    assert _classify_refusal("the model could not be reached to write the function: boom") == "transport"
    assert _classify_refusal("the model ran out of authoring steps without finishing") == "no_script"


# --------------------------------------------------------------------------- #
# The runner speaks the runtime contract                                       #
# --------------------------------------------------------------------------- #
def test_the_runner_accepts_an_honest_error_as_the_contract():
    report = BenchScriptRunner().run(
        "from _oolu_runtime import emit_error\n"
        "emit_error('the data is unreachable')\n",
        {},
    )
    assert not report.ok
    assert report.kind == "honest_error"
    assert "unreachable" in report.error


def test_a_script_that_never_emits_is_a_contract_violation():
    report = BenchScriptRunner().run("print('hello')\n", {})
    assert report.kind == "contract_violation"


def test_a_crashing_script_is_a_script_error():
    report = BenchScriptRunner().run("raise RuntimeError('boom')\n", {})
    assert report.kind == "script_error"
    assert "boom" in report.error


def test_a_hanging_script_times_out():
    report = BenchScriptRunner(timeout_s=1.0).run(
        "import time\ntime.sleep(30)\n", {}
    )
    assert report.kind == "timeout"


def test_the_web_broker_refuses_and_the_contract_survives():
    report = BenchScriptRunner().run(
        "from _oolu_runtime import emit_result, http_request\n"
        "answer = http_request('https://api.example.com/x')\n"
        "emit_result({'status': answer['status'], 'why': answer['error']})\n",
        {},
    )
    assert report.ok, report.error
    assert report.payload["status"] == 0
    assert "no web hosts" in report.payload["why"]


def test_dependency_healing_through_an_injected_installer():
    def install(package: str, target: Path) -> bool:
        target.mkdir(parents=True, exist_ok=True)
        (target / "fakemod_bench.py").write_text("VALUE = 7\n", encoding="utf-8")
        return package == "fakemod_bench"

    runner = BenchScriptRunner(installer=install)
    report = runner.run(
        "import fakemod_bench\n"
        "from _oolu_runtime import emit_result\n"
        "emit_result({'value': fakemod_bench.VALUE})\n",
        {},
    )
    assert report.ok, report.error
    assert report.healed == ("fakemod_bench",)
    assert report.payload == {"value": 7}


def test_a_failed_heal_is_classified_missing_dependency():
    runner = BenchScriptRunner(installer=lambda package, target: False)
    report = runner.run("import fakemod_absent\n", {})
    assert report.kind == "missing_dependency"
    assert not report.healed


# --------------------------------------------------------------------------- #
# The effort books                                                             #
# --------------------------------------------------------------------------- #
def test_the_meter_books_finish_reason_and_context_size():
    meter = ModelCallMeter()
    record = meter.record("bench.effort", _Telemetry())
    assert record.finish_reason == "length"
    assert record.context_chars == 3600


def test_older_telemetry_shapes_still_meter_without_the_new_fields():
    @dataclass
    class Legacy:  # the routing gateway's SynthesisResult shape
        model: str = "old"
        tier: str = "fast"
        prompt_tokens: int = 10
        completion_tokens: int = 20
        duration_s: float = 0.5

    record = ModelCallMeter().record("bench.effort", Legacy())
    assert record.finish_reason == ""
    assert record.context_chars == 0
