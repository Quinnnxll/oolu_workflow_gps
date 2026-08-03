"""F1 — the program author: plan → module loop → deterministic dispatcher."""

from __future__ import annotations

import json

from oolu.cache import LocalScriptCache
from oolu.programbuilder import (
    ProgramAuthor,
    plan_program,
    render_dispatcher,
)
from oolu.runtime import NodeScriptRunner
from oolu.runtime.isolation import SubprocessBackend
from oolu.skills.program import parse_program_spec


# --------------------------------------------------------------------------- #
# A scripted model: keyed replies by what the prompt is asking for.            #
# --------------------------------------------------------------------------- #
class ScriptedModel:
    """Replies chosen by matching a substring of the user message —
    the plan once, then each module by its path. Calls are counted."""

    def __init__(self, replies: dict[str, str], *, default: str = ""):
        self._replies = replies
        self._default = default
        self.calls = 0

    def reply(self, messages: list[dict]) -> str:
        self.calls += 1
        user = messages[-1]["content"]
        for needle, answer in self._replies.items():
            if needle in user:
                return answer
        return self._default


PLAN = json.dumps(
    {
        "modules": [
            {
                "path": "lib/compute.py",
                "purpose": "compute the answer",
                "check": "tests/check_compute.py",
                "api": [{"name": "answer", "takes": [], "returns": "str"}],
            },
            {
                "path": "lib/report.py",
                "purpose": "shape the result",
                "depends": ["lib/compute.py"],
                "check": "tests/check_report.py",
            },
        ],
        "operations": [{"name": "main", "entry": "lib.report:build"}],
        "interface": {
            "operation": "main",
            "ports": [{"name": "summary", "type": "str"}],
        },
        "inputs": [],
    }
)

COMPUTE = (
    "```python\n"
    "def answer():\n"
    "    return 'the ' + 'answer'\n"
    "```\n"
    "```python\n"
    "from lib.compute import answer\n"
    "assert answer() == 'the answer'\n"
    "from _oolu_runtime import emit_result\n"
    "emit_result({'checked': 'lib/compute.py'})\n"
    "```\n"
)
REPORT = (
    "```python\n"
    "from lib.compute import answer\n"
    "def build(inputs):\n"
    "    return {'summary': answer()}\n"
    "```\n"
    "```python\n"
    "from lib.report import build\n"
    "assert build({})['summary'] == 'the answer'\n"
    "from _oolu_runtime import emit_result\n"
    "emit_result({'checked': 'lib/report.py'})\n"
    "```\n"
)


def _scripted() -> ScriptedModel:
    return ScriptedModel(
        {
            "Plan a program": PLAN,
            "lib/compute.py": COMPUTE,
            "lib/report.py": REPORT,
        }
    )


def _verify(tmp_path):
    return NodeScriptRunner(
        SubprocessBackend(), LocalScriptCache(tmp_path / "scripts.db")
    ).verify_function


# --------------------------------------------------------------------------- #
# Planning.                                                                    #
# --------------------------------------------------------------------------- #
def test_plan_program_returns_spec_and_io():
    spec, io, problem = plan_program(_scripted(), "compute and report")
    assert problem == "" and spec is not None
    assert [m.path for m in spec.modules] == ["lib/compute.py", "lib/report.py"]
    assert io["outputs"] == [{"name": "summary", "type": "str"}]
    assert io["inputs"] == []


def test_plan_refuses_conversation_and_single_module():
    _, _, chat = plan_program(ScriptedModel({"Plan a program": "NO_TASK"}), "hi")
    assert "conversation" in chat
    one = json.dumps(
        {
            "modules": [{"path": "lib/only.py"}],
            "operations": [{"name": "main", "entry": "lib.only:run"}],
            "interface": {"operation": "main"},
        }
    )
    _, _, problem = plan_program(ScriptedModel({"Plan a program": one}), "x")
    assert "at least two internal modules" in problem


def test_plan_refuses_a_spec_that_breaks_the_laws():
    bad = json.dumps({"interface": []})
    _, _, problem = plan_program(ScriptedModel({"Plan a program": bad}), "x")
    assert "ONE unified interface" in problem


# --------------------------------------------------------------------------- #
# The deterministic dispatcher.                                                #
# --------------------------------------------------------------------------- #
def test_dispatcher_omits_bindings_read_when_no_inputs():
    spec, io, _ = plan_program(_scripted(), "x")
    script = render_dispatcher(spec, io)
    assert "bindings.json" not in script  # zero inputs: the birth wall's rule
    assert "importlib.import_module" in script
    assert 'emit_result(' in script


def test_dispatcher_reads_bindings_when_inputs_declared():
    plan = json.loads(PLAN)
    plan["inputs"] = [{"name": "source", "type": "str", "label": "Which file?"}]
    spec, io, _ = plan_program(
        ScriptedModel({"Plan a program": json.dumps(plan)}), "x"
    )
    script = render_dispatcher(spec, io)
    assert 'open("bindings.json"' in script


def test_dispatcher_is_never_model_written_and_dispatches_by_operation():
    spec, io, _ = plan_program(_scripted(), "x")
    script = render_dispatcher(spec, io)
    assert '"main": "lib.report:build"' in script
    assert '_DEFAULT = "main"' in script


# --------------------------------------------------------------------------- #
# The build: plan -> module loop -> tree.                                      #
# --------------------------------------------------------------------------- #
def test_build_produces_a_verified_tree(tmp_path):
    model = _scripted()
    build = ProgramAuthor(model, verify=_verify(tmp_path)).build("compute and report")
    assert build.ok, build.problem
    assert set(build.files) >= {
        "lib/__init__.py",
        "lib/compute.py",
        "lib/report.py",
        "tests/check_compute.py",
        "tests/check_report.py",
    }
    # 1 plan + 1 per module (both passed first try) = 3 consultations.
    assert build.consultations == 3
    assert model.calls == 3
    assert any("verified" in s for s in build.states)
    assert build.script and "importlib" in build.script


def test_build_authors_modules_in_dependency_order(tmp_path):
    order: list[str] = []

    class RecordingModel(ScriptedModel):
        def reply(self, messages):
            user = messages[-1]["content"]
            if "Write the module: lib/compute.py" in user:
                order.append("compute")
            elif "Write the module: lib/report.py" in user:
                order.append("report")
            return super().reply(messages)

    model = RecordingModel(
        {
            "Plan a program": PLAN,
            "lib/compute.py": COMPUTE,
            "lib/report.py": REPORT,
        }
    )
    build = ProgramAuthor(model, verify=_verify(tmp_path)).build("x")
    assert build.ok, build.problem
    # report depends on compute, so compute authors first.
    assert order == ["compute", "report"]


def test_module_check_failure_repairs_then_proceeds(tmp_path):
    bad_compute = (
        "```python\n"
        "def answer():\n"
        "    return 'wrong'\n"
        "```\n"
        "```python\n"
        "from lib.compute import answer\n"
        "assert answer() == 'the answer'\n"
        "from _oolu_runtime import emit_result\n"
        "emit_result({'checked': 'lib/compute.py'})\n"
        "```\n"
    )

    class RepairingModel:
        def __init__(self):
            self.calls = 0
            self._compute_attempts = 0

        def reply(self, messages):
            self.calls += 1
            user = messages[-1]["content"]
            if "Plan a program" in user:
                return PLAN
            if "gate refused" in user:  # the repair turn
                return COMPUTE  # the corrected module + check
            if "lib/compute.py" in user:
                self._compute_attempts += 1
                return bad_compute
            if "lib/report.py" in user:
                return REPORT
            return ""

    model = RepairingModel()
    build = ProgramAuthor(model, verify=_verify(tmp_path)).build("x")
    assert build.ok, build.problem
    # plan + compute(author) + compute(repair) + report = 4 consultations.
    assert build.consultations == 4
    assert any("verified" in s for s in build.states)


def test_module_exhausts_repairs_and_refuses(tmp_path):
    bad_compute = (
        "```python\n"
        "def answer():\n"
        "    return 'wrong'\n"
        "```\n"
        "```python\n"
        "from lib.compute import answer\n"
        "assert answer() == 'the answer'\n"
        "from _oolu_runtime import emit_result\n"
        "emit_result({'checked': 'lib/compute.py'})\n"
        "```\n"
    )
    # Always returns the failing module, even on repair — exhausts the bound.
    model = ScriptedModel(
        {
            "Plan a program": PLAN,
            "lib/compute.py": bad_compute,
            "gate refused": bad_compute,
        }
    )
    build = ProgramAuthor(model, verify=_verify(tmp_path)).build("x")
    assert not build.ok
    assert "repair rounds" in build.problem
    assert any("exhausted" in s for s in build.states)


def test_build_refuses_a_mocked_module_before_the_sandbox(tmp_path):
    mocked = (
        "```python\n"
        "def answer():\n"
        "    return 'placeholder data'\n"
        "```\n"
        "```python\n"
        "from _oolu_runtime import emit_result\n"
        "emit_result({'checked': 'lib/compute.py'})\n"
        "```\n"
    )
    model = ScriptedModel(
        {"Plan a program": PLAN, "lib/compute.py": mocked, "gate refused": mocked}
    )
    build = ProgramAuthor(model, verify=_verify(tmp_path)).build("x")
    assert not build.ok
    assert "mock" in build.problem or "placeholder" in build.problem


# --------------------------------------------------------------------------- #
# The explicit-request routing (before single-file authoring).                 #
# --------------------------------------------------------------------------- #
def test_explicit_program_request_is_recognized_and_scoped():
    from oolu.gateway.app import (
        explicit_node_build_goal,
        explicit_program_build_goal,
    )

    goal = explicit_program_build_goal(
        "build me a program that ingests invoices and reports totals"
    )
    assert goal == "ingests invoices and reports totals"
    assert (
        explicit_program_build_goal("build me a program node for stock tracking")
        == "stock tracking"
    )
    # A plain node request is NOT a program request.
    assert explicit_program_build_goal("build me a node that sorts files") is None
    # A program request still matches the node regex (it contains no "node"),
    # so ordering matters: the program check must run FIRST. The node regex
    # does not spuriously claim a bare "program" request.
    assert explicit_node_build_goal("build me a program that does X") is None
