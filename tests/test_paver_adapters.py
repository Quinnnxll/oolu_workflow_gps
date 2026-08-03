"""W3 — adapter synthesis: negotiate a junction, pour a tested bridge.

The negotiator is a pure classifier (``direct`` | ``mappable`` |
``convertible`` | ``none``); the synthesizer pours a rename as a
byte-stable template (no model) and a shape change as a model-authored
script, both earned by execution; the PaverAgent splices a bridged
junction into the web so a near-miss becomes a paved(adapted) route.
Advice may propose pairs, but only a passing test creates an edge.
"""

from __future__ import annotations

from oolu.paver import (
    AdapterBuild,
    AdapterSpec,
    AdapterSynthesizer,
    ContractNegotiator,
    PaverAgent,
    RehearsalResult,
    render_mapping_adapter,
)
from oolu.paver.discovery import SurveyNode
from oolu.skills.contract import NodeContract, ScriptBody, Slot


# --------------------------------------------------------------------------- #
# Stubs.                                                                       #
# --------------------------------------------------------------------------- #
class StubModel:
    """A scripted ``reply(messages) -> str`` — the seated model's stand-in."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def reply(self, messages):
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return reply


class DeadModel:
    """A model that must never be consulted — its use is the test's failure."""

    def reply(self, messages):  # pragma: no cover - asserted un-called
        raise AssertionError("the model was consulted on a model-free path")


def _ok_verify(*args, **kwargs):
    return {"ok": True, "honest_error": False, "error": "", "healed": []}


def _failing_verify(*args, **kwargs):
    return {
        "ok": False, "honest_error": False,
        "error": "output contract violated — port missing", "healed": [],
    }


def _script_node(name, *, consumes=None, produces=None) -> NodeContract:
    return NodeContract(
        name=name,
        provenance="synthesized",
        consumes=list(consumes or []),
        produces=list(produces or []),
        body=ScriptBody(goal=f"do {name}"),
    )


def _shape_reply(consumer_name: str, variant: str = "") -> str:
    marker = f"# {variant}\n" if variant else ""
    return (
        "```python\n"
        "import json\n"
        "from _oolu_runtime import emit_result\n"
        + marker
        + 'with open("bindings.json", encoding="utf-8") as fh:\n'
        "    b = json.load(fh)\n"
        f'emit_result({{"{consumer_name}": int(b["{consumer_name}"])}})\n'
        "```\n"
    )


# --------------------------------------------------------------------------- #
# The negotiator — pure classification.                                       #
# --------------------------------------------------------------------------- #
def test_negotiator_classifies_the_four_verdicts():
    neg = ContractNegotiator()
    # direct: name + type (+ role) agree exactly.
    a = Slot(name="path", value_type="str", role="path")
    assert neg.negotiate(a, a).verdict == "direct"
    # mappable: same type+role, a different name.
    produced = Slot(name="folder", value_type="str", role="path")
    consumed = Slot(name="path", value_type="str", role="path")
    result = neg.negotiate(produced, consumed)
    assert result.verdict == "mappable"
    assert result.produced_slot == "folder" and result.consumed_slot == "path"
    # convertible: same name, a different type.
    p2 = Slot(name="count", value_type="str")
    c2 = Slot(name="count", value_type="number")
    assert neg.negotiate(p2, c2).verdict == "convertible"
    # none: unrelated — the type system offers no bridge.
    p3 = Slot(name="invoice", value_type="json")
    c3 = Slot(name="total", value_type="number")
    assert neg.negotiate(p3, c3).verdict == "none"


def test_negotiator_role_disagreement_is_no_bridge():
    neg = ContractNegotiator()
    # Same type, different name, but roles DISAGREE: not a rename.
    produced = Slot(name="folder", value_type="str", role="path")
    consumed = Slot(name="city", value_type="str", role="place")
    assert neg.negotiate(produced, consumed).verdict == "none"


# --------------------------------------------------------------------------- #
# Rename → deterministic template, no model.                                  #
# --------------------------------------------------------------------------- #
def test_rename_adapter_is_byte_stable_and_model_free():
    first = render_mapping_adapter("folder", "path")
    second = render_mapping_adapter("folder", "path")
    assert first == second  # byte-stable
    # The template reads the produced name and emits under the consumer's.
    assert 'emit_result({"path": _bindings["folder"]})' in first

    spec = AdapterSpec(
        produced=Slot(name="folder", value_type="str", role="path"),
        consumed=Slot(name="path", value_type="str", role="path"),
        verdict="mappable",
        sample="/data/invoices",
    )
    # A DeadModel proves the rename path never consults the model.
    synth = AdapterSynthesizer(verify=_ok_verify, model=DeadModel())
    result = synth.author(spec)
    assert result.ok and result.templated and result.verdict == "mappable"
    assert result.rounds == 0


def test_rename_adapter_that_fails_verify_makes_no_edge():
    spec = AdapterSpec(
        produced=Slot(name="folder", value_type="str", role="path"),
        consumed=Slot(name="path", value_type="str", role="path"),
        verdict="mappable",
        sample="/data",
    )
    synth = AdapterSynthesizer(verify=_failing_verify, model=None)
    result = synth.author(spec)
    assert not result.ok  # a template that cannot pass is refused, no repair


# --------------------------------------------------------------------------- #
# Shape → model-authored, verified by execution.                              #
# --------------------------------------------------------------------------- #
def test_shape_adapter_passes_verify_with_a_stub_model():
    spec = AdapterSpec(
        produced=Slot(name="count", value_type="str"),
        consumed=Slot(name="count", value_type="number"),
        verdict="convertible",
        sample="42",
    )
    model = StubModel([_shape_reply("count")])
    synth = AdapterSynthesizer(verify=_ok_verify, model=model)
    result = synth.author(spec)
    assert result.ok and not result.templated and result.verdict == "convertible"
    assert model.calls == 1  # one draft, no repair needed


def test_shape_adapter_exhausts_repairs_then_refuses():
    spec = AdapterSpec(
        produced=Slot(name="count", value_type="str"),
        consumed=Slot(name="count", value_type="number"),
        verdict="convertible",
        sample="42",
    )
    # Verify never passes; the model returns DISTINCT code each round (an
    # identical repair would read as "gave up" and stop early) → the loop
    # runs 1 draft + 2 repairs before refusing.
    model = StubModel([
        _shape_reply("count", "draft"),
        _shape_reply("count", "round-1"),
        _shape_reply("count", "round-2"),
    ])
    synth = AdapterSynthesizer(verify=_failing_verify, model=model, max_repairs=2)
    result = synth.author(spec)
    assert not result.ok and result.verdict == "convertible"
    assert result.rounds == 2
    assert model.calls == 3  # draft + 2 repair rounds, then refuse


def test_shape_adapter_needs_a_model():
    spec = AdapterSpec(
        produced=Slot(name="count", value_type="str"),
        consumed=Slot(name="count", value_type="number"),
        verdict="convertible",
        sample="1",
    )
    synth = AdapterSynthesizer(verify=_ok_verify, model=None)
    assert not synth.author(spec).ok


# --------------------------------------------------------------------------- #
# Containment — absurd advice cannot create an edge.                          #
# --------------------------------------------------------------------------- #
def test_none_verdict_builds_nothing():
    # Whatever a paver.match model proposes, the negotiator disposes: two
    # unrelated slots classify "none", and a "none" spec builds no adapter,
    # so no test ever runs and no edge is ever created.
    neg = ContractNegotiator()
    produced = Slot(name="invoice", value_type="json")
    consumed = Slot(name="total", value_type="number")
    verdict = neg.negotiate(produced, consumed).verdict
    assert verdict == "none"
    spec = AdapterSpec(produced=produced, consumed=consumed, verdict=verdict)
    # Even handed a live model, a "none" junction builds nothing.
    synth = AdapterSynthesizer(verify=_ok_verify, model=DeadModel())
    assert not synth.author(spec).ok


# --------------------------------------------------------------------------- #
# The agent — a near-miss web paves once the junction is bridged.             #
# --------------------------------------------------------------------------- #
def _near_miss_survey_nodes():
    producer = _script_node(
        "src", produces=[Slot(name="folder", value_type="str", role="path")]
    )
    consumer = _script_node(
        "dst", consumes=[Slot(name="path", value_type="str", role="path")]
    )
    return [
        SurveyNode(key="src", contract=producer, anchor_kind="webhook"),
        SurveyNode(key="dst", contract=consumer),
    ]


def test_near_miss_web_paves_once_the_adapter_bridges_it():
    nodes = _near_miss_survey_nodes()
    promoted: dict = {}

    def build_adapter(tenant, miss, src, tgt):
        # The gateway would negotiate + synthesize + land here; the stub
        # returns the adapter child (consumes produced, produces consumed).
        adapter = _script_node(
            "adapt-folder-to-path",
            consumes=[Slot(name=miss.produced_slot, value_type="str", role="path")],
            produces=[Slot(name=miss.consumed_slot, value_type="str", role="path")],
        )
        return AdapterBuild(node_id="adapter-1", contract=adapter, templated=True)

    def promote(tenant, web, contract):
        promoted["contract"] = contract
        return "web-node-1"

    agent = PaverAgent(
        rehearse=lambda contract: RehearsalResult(ok=True),
        promote=promote,
        build_adapter=build_adapter,
    )
    report = agent.tick("t", nodes)

    assert report.paved == [report.outcomes[0].web_id] or report.paved
    assert report.adapters_built == 1
    paved_outcomes = [o for o in report.outcomes if o.status == "paved"]
    assert len(paved_outcomes) == 1 and paved_outcomes[0].adapters == 1
    # The adapter was spliced into the promoted web: producer, consumer, adapter.
    children = promoted["contract"].body.nodes
    assert len(children) == 3
    assert any(
        [s.name for s in c.consumes] == ["folder"]
        and [s.name for s in c.produces] == ["path"]
        for c in children
    )


def test_unbridgeable_near_miss_fails_the_web_and_files_negative():
    nodes = _near_miss_survey_nodes()
    negatives: list = []

    agent = PaverAgent(
        rehearse=lambda contract: RehearsalResult(ok=True),
        promote=lambda tenant, web, contract: "unused",
        negative=lambda tenant, web, reason: negatives.append(reason),
        build_adapter=lambda tenant, miss, src, tgt: None,  # cannot bridge
    )
    report = agent.tick("t", nodes)

    assert report.paved == []
    assert report.adapters_built == 0
    failed = [o for o in report.outcomes if o.status == "adapter-failed"]
    assert len(failed) == 1
    assert negatives and "could not adapt" in negatives[0]


def test_near_miss_web_is_skipped_without_an_adapter_builder():
    # The W2 posture preserved: with no build_adapter port, a near-miss web
    # is skipped, never forced.
    nodes = _near_miss_survey_nodes()
    agent = PaverAgent(
        rehearse=lambda contract: RehearsalResult(ok=True),
        promote=lambda tenant, web, contract: "unused",
    )
    report = agent.tick("t", nodes)
    assert report.paved == []
    skipped = [o for o in report.outcomes if o.status == "skipped"]
    assert skipped and "no adapter builder" in skipped[0].reason
