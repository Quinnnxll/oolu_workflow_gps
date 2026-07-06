"""Value patching: creative values enter workflows through declared holes.

The mechanical-design scenario, proven end to end: the scaffolding of a
CAD workflow (open the application, open the file, select the tool) is
deterministic slot-chained actions, and at the moment the creative work
starts the run pulls the node's declared input manifest — names, types,
defaults, hard bounds — and a smart plugin (an LLM ``ValuePatcher``) fills
the values in ONE batched call. User-provided values outrank the patcher,
the patcher outranks the declared defaults, every value is boxed by its
declaration (numbers clamp, hallucinated choices revert, unknown names
drop), the metered patch cost rides the budget verdict, and the geometry
verification stays sovereign over whatever was filled.
"""

from __future__ import annotations

import shutil
from math import pi, sin

import pytest
from test_contract_run import _build
from test_gateway_market import _contribute_and_publish
from test_http_gateway import _req

from oolu.billing.model_calls import ModelCallMeter
from oolu.domains.cad import (
    PARAMETRIC_PLATE_INPUTS,
    PARAMETRIC_PLATE_SCAD,
    PARAMETRIC_PLATE_SPEC,
    OpenSCADExecutor,
    manifold_report,
    parametric_plate_pack,
    rect_plate_with_hole,
    signed_volume,
)
from oolu.knowledge import TraceStore
from oolu.orchestrator.patchers import (
    PATCH_PURPOSE,
    PATCH_SYSTEM_PROMPT,
    DefaultValuePatcher,
    GatewayValuePatcher,
    ValuePatch,
    parse_patch,
    patch_or_defaults,
)
from oolu.routing.gateway import FakeGateway, GatewayError, SynthesisResult
from oolu.skills import ActionsBody, NodeContract
from oolu.skills.contract import SubgraphBody, ValueInput
from oolu.skills.inputs import (
    bind_inputs,
    inputs_manifest,
    resolve_values,
    validate_value,
)
from oolu.skills.models import (
    ActionEvent,
    ExecutionOutcome,
    ExecutionStatus,
)

WIDTH = ValueInput(
    name="width", description="mm", default=40.0, minimum=30.0, maximum=60.0
)
FINISH = ValueInput(
    name="finish",
    value_type="choice",
    default="matte",
    choices=["matte", "gloss"],
)
LABEL = ValueInput(name="label", value_type="string", default="part-a")


def _action(operation="render", parameters=None, adapter="cad"):
    return ActionEvent(
        correlation_id="c",
        adapter=adapter,
        operation=operation,
        parameters=parameters or {},
    )


def _node(name, *, inputs=(), parameters=None, node_id=None):
    return NodeContract(
        id=node_id or f"lib.{name}",
        name=name,
        inputs=list(inputs),
        body=ActionsBody(actions=[_action(parameters=parameters)]),
    )


# --------------------------------------------------------------------------- #
# The manifest: pulling the list of inputs needed, with defaults and bounds.   #
# --------------------------------------------------------------------------- #
def test_manifest_uses_bare_names_for_a_single_node():
    manifest = inputs_manifest(_node("plate", inputs=[WIDTH, FINISH]))
    assert [(m.qualified, m.node_name) for m in manifest] == [
        ("width", "plate"),
        ("finish", "plate"),
    ]


def test_manifest_qualifies_each_child_of_a_subgraph():
    child_a = _node("open app")
    child_b = _node("model plate", inputs=[WIDTH], node_id="lib.model")
    graph = NodeContract(
        id="g", name="goal", body=SubgraphBody(nodes=[child_a, child_b])
    )
    manifest = inputs_manifest(graph)
    assert [m.qualified for m in manifest] == ["model plate.width"]
    assert manifest[0].node_id == "lib.model"


def test_two_children_sharing_a_name_with_inputs_are_refused():
    twin_a = _node("plate", inputs=[WIDTH], node_id="lib.a")
    twin_b = _node("plate", inputs=[WIDTH], node_id="lib.b")
    graph = NodeContract(id="g", name="goal", body=SubgraphBody(nodes=[twin_a, twin_b]))
    with pytest.raises(ValueError, match="unattributable"):
        inputs_manifest(graph)


# --------------------------------------------------------------------------- #
# Boxing: the declaration always wins over whatever a filler offers.           #
# --------------------------------------------------------------------------- #
def test_numbers_clamp_into_their_declared_bounds():
    assert validate_value(WIDTH, 45.5) == 45.5
    assert validate_value(WIDTH, 9000) == 60.0  # too big: clamped
    assert validate_value(WIDTH, -3) == 30.0  # too small: clamped
    assert validate_value(WIDTH, "50") == 50.0  # numeric text coerces
    with pytest.raises(ValueError):
        validate_value(WIDTH, "wide-ish")  # garbage raises; caller decides


def test_hallucinated_choices_fall_back_to_the_default():
    assert validate_value(FINISH, "gloss") == "gloss"
    assert validate_value(FINISH, "chrome") == "matte"  # not in the set
    orphan = ValueInput(name="f", value_type="choice", choices=["a", "b"])
    with pytest.raises(ValueError, match="not one of"):
        validate_value(orphan, "c")  # no default to fall back to


def test_resolution_precedence_and_strictness():
    manifest = inputs_manifest(_node("plate", inputs=[WIDTH, FINISH, LABEL]))
    # Provided beats default; the rest take their defaults.
    resolved = resolve_values(manifest, {"width": 50})
    assert resolved == {"width": 50.0, "finish": "matte", "label": "part-a"}
    # A misspelled key is a caller error, not silence.
    with pytest.raises(ValueError, match="unknown inputs: widht"):
        resolve_values(manifest, {"widht": 50})
    resolve_values(manifest, {"widht": 50}, strict=False)  # unless asked
    # Garbage where a number belongs degrades to the honest default.
    assert resolve_values(manifest, {"width": "very"})["width"] == 40.0
    # Required with neither a value nor a default refuses to bind.
    bare = inputs_manifest(_node("plate", inputs=[ValueInput(name="width")]))
    with pytest.raises(ValueError, match="required and has no default"):
        resolve_values(bare, {})
    assert resolve_values(bare, {"width": 33}) == {"width": 33.0}


# --------------------------------------------------------------------------- #
# Binding: placeholders become concrete parameters before anything compiles.   #
# --------------------------------------------------------------------------- #
def test_dollar_input_replaces_the_whole_parameter_value():
    node = _node(
        "plate",
        inputs=[WIDTH, LABEL],
        parameters={"w": {"$input": "width"}, "name": {"$input": "label"}},
    )
    bound = bind_inputs(node, {"width": 44})
    assert bound.body.actions[0].parameters == {"w": 44.0, "name": "part-a"}


def test_dollar_template_fills_numeric_and_choice_holes():
    node = _node(
        "plate",
        inputs=[WIDTH, FINISH],
        parameters={"src": {"$template": "cube([{width}]); // {finish}"}},
    )
    bound = bind_inputs(node, {"width": 33.5, "finish": "gloss"})
    # Floats render via format(..., "g"): 33.5 stays 33.5, 40.0 would be 40.
    assert bound.body.actions[0].parameters["src"] == "cube([33.5]); // gloss"
    assert bind_inputs(node).body.actions[0].parameters["src"] == (
        "cube([40]); // matte"
    )


def test_free_strings_are_refused_inside_templates():
    node = _node(
        "plate",
        inputs=[LABEL],
        parameters={"src": {"$template": "echo {label};"}},
    )
    with pytest.raises(ValueError, match="injection"):
        bind_inputs(node)  # '"); do_evil(' stays unrepresentable


def test_a_placeholder_naming_an_undeclared_input_refuses_to_bind():
    node = _node("plate", parameters={"src": {"$template": "cube({width});"}})
    with pytest.raises(ValueError, match="undeclared input"):
        bind_inputs(node)
    node = _node("plate", parameters={"w": {"$input": "width"}})
    with pytest.raises(ValueError, match="undeclared input"):
        bind_inputs(node)


def test_binding_without_placeholders_is_the_identity():
    plain = _node("plate", parameters={"depth": 3, "tags": ["a", {"b": 1}]})
    assert bind_inputs(plain) == plain
    # ...and substitution reaches into nested dicts and lists when present.
    nested = _node(
        "plate",
        inputs=[WIDTH],
        parameters={"ops": [{"cut": {"$input": "width"}}, "polish"]},
    )
    bound = bind_inputs(nested, {"width": 31})
    assert bound.body.actions[0].parameters["ops"] == [{"cut": 31.0}, "polish"]


# --------------------------------------------------------------------------- #
# The patcher: one batched metered call, boxed on the way in, never blocking.  #
# --------------------------------------------------------------------------- #
class _TokenGateway:
    """A scripted Gateway whose completions carry token telemetry."""

    def __init__(self, text, *, prompt_tokens=200, completion_tokens=60):
        self._text = text
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self.calls = []

    @property
    def name(self):
        return "tokens"

    def complete(self, decision, prompt):
        self.calls.append((decision, prompt))
        return SynthesisResult(
            raw_text=self._text,
            script=None,
            model=decision.model,
            tier=decision.tier,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            finish_reason="stop",
        )


_PLATE_PATCH = """Here you go:
```json
{"width": 50, "depth": 25, "thickness": 100,
 "hole_radius": 3.5, "spindle_speed": 9000}
```"""


def _plate_manifest():
    render, _verify = parametric_plate_pack()
    return inputs_manifest(render)


def test_one_completion_fills_the_whole_manifest_and_is_metered():
    gateway = _TokenGateway(_PLATE_PATCH)
    meter = ModelCallMeter()
    patcher = GatewayValuePatcher(gateway, meter=meter)
    patch = patcher.patch(goal="design a plate", manifest=_plate_manifest())

    assert len(gateway.calls) == 1  # patch by patch = ONE batched call
    decision, prompt = gateway.calls[0]
    assert decision.max_tokens <= 768
    assert prompt.messages[0]["content"] == PATCH_SYSTEM_PROMPT
    ask = prompt.messages[1]["content"]
    assert ask.startswith("Goal: design a plate")
    for spec in PARAMETRIC_PLATE_INPUTS:  # every declared input rode along
        assert f"- name: {spec.name}" in ask
        assert f"bounds: [{spec.minimum}, {spec.maximum}]" in ask
    assert "default: 40.0" in ask

    # The values came back boxed: 100 clamped to 6, the invented
    # parameter dropped — the model cannot add what the node didn't declare.
    assert patch.values == {
        "width": 50.0,
        "depth": 25.0,
        "thickness": 6.0,
        "hole_radius": 3.5,
    }
    (charge,) = meter.charges(PATCH_PURPOSE)
    assert patch.cost == charge.cost > 0.0


def test_an_empty_manifest_never_costs_a_model_call():
    gateway = FakeGateway([])  # raises if anyone calls it
    patcher = GatewayValuePatcher(gateway)
    assert patcher.patch(goal="g", manifest=[]) == ValuePatch()
    assert gateway.calls == []


def test_parse_patch_survives_prose_arrays_and_garbage():
    manifest = _plate_manifest()
    assert parse_patch(None, manifest) == {}
    assert parse_patch("I would pick something sturdy.", manifest) == {}
    assert parse_patch("```json\n[40, 20]\n```", manifest) == {}
    # An unusable individual value vanishes; usable siblings survive.
    text = '```json\n{"width": null, "depth": "22"}\n```'
    assert parse_patch(text, manifest) == {"depth": 22.0}


def test_a_dead_or_missing_patcher_degrades_to_declared_defaults():
    manifest = _plate_manifest()
    assert patch_or_defaults(None, goal="g", manifest=manifest) == ValuePatch()

    dead = GatewayValuePatcher(FakeGateway([GatewayError("endpoint down")]))
    assert patch_or_defaults(dead, goal="g", manifest=manifest) == ValuePatch()

    free = DefaultValuePatcher()
    assert free.patch(goal="g", manifest=manifest) == ValuePatch()


# --------------------------------------------------------------------------- #
# The parametric plate pack: bounds-derived spec, proven against closed forms. #
# --------------------------------------------------------------------------- #
def _hole_area(r, n=64):
    """Cross-section of OpenSCAD's cylinder($fn=n): the inscribed n-gon."""
    return (n / 2.0) * r * r * sin(2.0 * pi / n)


def test_template_holes_and_declared_inputs_agree_exactly():
    from oolu.skills.inputs import _HOLE_RE

    holes = set(_HOLE_RE.findall(PARAMETRIC_PLATE_SCAD))
    declared = {spec.name for spec in PARAMETRIC_PLATE_INPUTS}
    assert holes == declared == {"width", "depth", "thickness", "hole_radius"}
    for spec in PARAMETRIC_PLATE_INPUTS:  # every hole is a bounded number
        assert spec.value_type == "number"
        assert spec.minimum is not None and spec.maximum is not None
        assert spec.minimum <= spec.default <= spec.maximum


def test_the_spec_admits_every_fill_the_bounds_admit():
    """volume = t·(w·d − A₆₄(r)) is monotone in each input, so checking the
    extreme corners of the declared box covers the whole box."""
    by_name = {spec.name: spec for spec in PARAMETRIC_PLATE_INPUTS}
    w, d, t, r = (
        by_name["width"],
        by_name["depth"],
        by_name["thickness"],
        by_name["hole_radius"],
    )
    smallest = t.minimum * (w.minimum * d.minimum - _hole_area(r.maximum))
    largest = t.maximum * (w.maximum * d.maximum - _hole_area(r.minimum))
    assert PARAMETRIC_PLATE_SPEC.min_volume < smallest  # ≈ 1199.4
    assert largest < PARAMETRIC_PLATE_SPEC.max_volume  # ≈ 10724.7
    # The fit envelope brackets the input bounds the same way.
    assert PARAMETRIC_PLATE_SPEC.fits_within == (60.01, 30.01, 6.01)
    assert PARAMETRIC_PLATE_SPEC.at_least == (29.99, 14.99, 2.99)
    # The hole can never breach an edge (2r ≤ 8 < 15 ≤ min(w, d)) and the
    # cutter spans [-1, t+1], so EVERY admissible fill is genus 1.
    assert 2 * r.maximum < d.minimum
    assert PARAMETRIC_PLATE_SPEC.expected_genus == 1


def test_the_reference_plate_matches_the_closed_forms_across_the_box():
    """The test instrument itself is a theorem: watertight, genus 1, and
    volume exactly t·(w·d − A₆₄(r)) at the corners and inside the box."""
    fills = [
        (30.0, 15.0, 3.0, 4.0),  # the smallest admissible plate
        (60.0, 30.0, 6.0, 2.0),  # the largest
        (40.0, 20.0, 4.0, 3.0),  # the defaults
        (33.3, 15.7, 5.2, 3.9),  # an awkward interior point
    ]
    for w, d, t, r in fills:
        mesh = rect_plate_with_hole(w, d, t, r)
        report = manifold_report(mesh)
        assert report.boundary_edges == 0
        assert report.non_manifold_edges == 0
        assert report.misoriented_edges == 0
        assert (report.components, report.genus) == (1, 1)
        assert signed_volume(mesh) == pytest.approx(
            t * (w * d - _hole_area(r)), rel=1e-9
        )
    with pytest.raises(ValueError, match="breach"):
        rect_plate_with_hole(20.0, 8.0, 4.0, 4.0)  # 2r == depth


# --------------------------------------------------------------------------- #
# THE SCENARIO: open app → open file → select tool → creative values → verify. #
# --------------------------------------------------------------------------- #
APP = {"name": "cad_app", "value_type": "str", "role": "app-session"}
PART = {"name": "part_file", "value_type": "path", "role": "open-file"}
TOOL = {"name": "active_tool", "value_type": "str", "role": "tool"}
STL = {"name": "plate_stl", "value_type": "path", "role": "stl"}
REPORT = {"name": "plate_report", "value_type": "json", "role": "geometry-report"}

# A renderer stub speaking OpenSCAD's CLI that honors the SCAD it is
# given: it parses the concrete dimensions the template was bound with
# and emits exactly the solid that SCAD describes (cube minus inscribed
# 64-gon cylinder) — so verification measures the patched values, not a
# canned fixture.
_PARSING_STUB = r"""
import re
import sys

from oolu.domains.cad import rect_plate_with_hole, write_binary_stl

args = sys.argv[1:]
out = args[args.index("-o") + 1]
with open(args[-1], encoding="utf-8") as handle:
    source = handle.read()
w, d, t = (
    float(x)
    for x in re.search(
        r"cube\(\[([0-9.]+), ([0-9.]+), ([0-9.]+)\]\)", source
    ).groups()
)
r = float(re.search(r"r = ([0-9.]+)\)", source).group(1))
with open(out, "wb") as sink:
    sink.write(write_binary_stl(rect_plate_with_hole(w, d, t, r)))
"""


class _AppExecutor:
    """The desktop-scaffolding adapter: opens apps, files, tools."""

    name = "app"

    def __init__(self):
        self.operations = []

    def capabilities(self):
        return frozenset({"open_app", "open_file", "select_tool"})

    def execute(self, action, *, idempotency_key):
        self.operations.append(action.operation)
        return ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=ExecutionStatus.SUCCEEDED,
            evidence={"operation": action.operation, **action.parameters},
        )

    def cancel(self, idempotency_key):
        return None


def _scenario_market(tmp_path, *, value_patcher=None):
    """The five-node mechanical-design market behind one gateway."""
    import sys

    stub = tmp_path / "fake_openscad.py"
    stub.write_text(_PARSING_STUB, encoding="utf-8")
    workdir = tmp_path / "cadwork"
    app_executor = _AppExecutor()
    executors = {
        "app": app_executor,
        "cad": OpenSCADExecutor(workdir, binary=[sys.executable, str(stub)]),
    }
    traces = TraceStore(tmp_path / "traces.db")
    app, conn, ident, registry, *_rest = _build(
        tmp_path,
        executors=executors,
        trace_store=traces,
        value_patcher=value_patcher,
    )

    def scaffold(operation, parameters):
        return [
            ActionEvent(
                correlation_id="mech",
                adapter="app",
                operation=operation,
                parameters=parameters,
            )
        ]

    render, verify = parametric_plate_pack()
    nodes = [
        (
            "open cad application",
            scaffold("open_app", {"application": "openscad"}),
            [],
            [APP],
            None,
        ),
        (
            "open part file",
            scaffold("open_file", {"path": "plate_project.scad"}),
            [APP],
            [PART],
            None,
        ),
        (
            "select extrude tool",
            scaffold("select_tool", {"tool": "extrude"}),
            [PART],
            [TOOL],
            None,
        ),
        (
            "model parametric plate",
            list(render.body.actions),
            [TOOL],
            [STL],
            [spec.model_dump(mode="json") for spec in PARAMETRIC_PLATE_INPUTS],
        ),
        ("verify plate geometry", list(verify.body.actions), [STL], [REPORT], None),
    ]
    for index, (name, actions, consumes, produces, inputs) in enumerate(nodes):
        _contribute_and_publish(
            app,
            ident,
            registry,
            name=name,
            noder=f"noder-{index}",
            price=0.05,
            actions=actions,
            consumes=consumes,
            produces=produces,
            inputs=inputs,
        )
    return app, conn, ident, app_executor, workdir, traces


def _assemble_plate(app, ident):
    resp = app.handle(
        _req(
            "POST",
            "/v1/market/assemble",
            token=ident.token("consumer", "t2"),
            body={"goal": {"name": "design-mounting-plate", "want": [REPORT]}},
        )
    )
    assert resp.status == 200 and resp.body["complete"], resp.body
    return resp.body


def _run_contract(app, ident, contract, *, inputs=None, key=None):
    body = {"contract": contract}
    if inputs is not None:
        body["inputs"] = inputs
    return app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body=body,
            headers={"Idempotency-Key": key} if key else None,
        )
    )


def test_the_mechanical_design_scenario_end_to_end(tmp_path):
    """Open app → open file → select tool → LLM-patched dimensions →
    verified geometry, through the public gateway, with the patch metered,
    boxed, budget-charged, and the whole run learned by the trace store."""
    gateway = _TokenGateway(
        _PLATE_PATCH.replace('"width"', '"model parametric plate.width"')
        .replace('"depth"', '"model parametric plate.depth"')
        .replace('"thickness"', '"model parametric plate.thickness"')
        .replace('"hole_radius"', '"model parametric plate.hole_radius"')
    )
    meter = ModelCallMeter()
    patcher = GatewayValuePatcher(gateway, meter=meter)
    app, conn, ident, app_executor, workdir, traces = _scenario_market(
        tmp_path, value_patcher=patcher
    )

    # Assembly pulls the whole chain from slots alone — and the preview
    # lists exactly which values the run will need, with defaults and
    # bounds, before anything is spent.
    preview = _assemble_plate(app, ident)
    assert preview["selected"] == [
        "verify plate geometry",
        "model parametric plate",
        "select extrude tool",
        "open part file",
        "open cad application",
    ]
    needed = {entry["name"]: entry for entry in preview["inputs"]}
    assert set(needed) == {
        "model parametric plate.width",
        "model parametric plate.depth",
        "model parametric plate.thickness",
        "model parametric plate.hole_radius",
    }
    assert needed["model parametric plate.width"]["default"] == 40.0
    assert needed["model parametric plate.width"]["maximum"] == 60.0

    resp = _run_contract(app, ident, preview["contract"], key="mech-1")
    assert resp.status == 200, resp.body
    assert resp.body["status"] == "succeeded"

    # The scaffolding ran deterministically, in slot order, before the
    # creative step ever executed.
    assert app_executor.operations == ["open_app", "open_file", "select_tool"]
    (run,) = traces.runs()
    assert [step.node_key for step in run.steps] == [
        "route:open cad application",
        "route:open part file",
        "route:select extrude tool",
        "route:model parametric plate",
        "route:verify plate geometry",
    ]
    assert run.success

    # ONE batched model call filled the manifest; its metered cost is
    # surfaced on the run and entered the budget verdict.
    assert len(gateway.calls) == 1
    assert resp.body["patch_cost"] == meter.total_cost(PATCH_PURPOSE) > 0.0
    assert resp.body["budget"] is not None

    # The patched values landed in the actual SCAD source — clamped
    # (thickness 100 → 6), with the invented parameter dropped and no
    # template hole left unfilled.
    scad = (workdir / "plate.scad").read_text(encoding="utf-8")
    assert "cube([50, 25, 6]);" in scad
    assert "r = 3.5);" in scad
    import re

    assert re.search(r"\{[A-Za-z_]", scad) is None  # no hole left unfilled
    assert "spindle_speed" not in scad

    # Verification measured the patched plate against the analytic spec:
    # volume = t·(w·d − A₆₄(r)) for the CLAMPED fill, and genus 1.
    verdicts = [o for o in resp.body["outcomes"] if "genus" in o["evidence"]]
    assert len(verdicts) == 1
    evidence = verdicts[0]["evidence"]
    assert evidence["passed"] is True
    assert evidence["genus"] == 1
    assert evidence["volume"] == pytest.approx(
        6.0 * (50.0 * 25.0 - _hole_area(3.5)), rel=1e-4
    )
    conn.close()


def test_user_values_outrank_the_patcher_and_typos_get_a_400(tmp_path):
    gateway = _TokenGateway('```json\n{"model parametric plate.width": 50}\n```')
    patcher = GatewayValuePatcher(gateway)
    app, conn, ident, _app_executor, workdir, _traces = _scenario_market(
        tmp_path, value_patcher=patcher
    )
    contract = _assemble_plate(app, ident)["contract"]

    resp = _run_contract(
        app, ident, contract, inputs={"model parametric plate.width": 33}
    )
    assert resp.status == 200 and resp.body["status"] == "succeeded"
    scad = (workdir / "plate.scad").read_text(encoding="utf-8")
    assert "cube([33, 20, 4]);" in scad  # user 33 beat the model's 50

    # A misspelled input name is refused before any money moves.
    bad = _run_contract(
        app, ident, contract, inputs={"model parametric plate.widht": 33}
    )
    assert bad.status == 400
    assert "unknown inputs" in bad.body["error"]["message"]
    conn.close()


def test_without_a_patcher_the_declared_defaults_run_and_verify(tmp_path):
    app, conn, ident, _app_executor, workdir, _traces = _scenario_market(tmp_path)
    contract = _assemble_plate(app, ident)["contract"]

    resp = _run_contract(app, ident, contract)
    assert resp.status == 200 and resp.body["status"] == "succeeded"
    assert resp.body["patch_cost"] == 0.0
    scad = (workdir / "plate.scad").read_text(encoding="utf-8")
    assert "cube([40, 20, 4]);" in scad  # the honest defaults
    evidence = next(
        o["evidence"] for o in resp.body["outcomes"] if "genus" in o["evidence"]
    )
    assert evidence["volume"] == pytest.approx(
        4.0 * (40.0 * 20.0 - _hole_area(3.0)), rel=1e-4
    )
    conn.close()


# --------------------------------------------------------------------------- #
# The real thing, when available.                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("openscad") is None, reason="OpenSCAD not installed")
def test_real_openscad_agrees_with_the_reference_plate(tmp_path):
    render, verify = parametric_plate_pack()
    bound = bind_inputs(render, {"width": 50, "thickness": 5, "hole_radius": 3.5})
    executor = OpenSCADExecutor(tmp_path)
    rendered = executor.execute(bound.body.actions[0], idempotency_key="real-r")
    assert rendered.status is ExecutionStatus.SUCCEEDED, rendered.error
    verified = executor.execute(verify.body.actions[0], idempotency_key="real-v")
    assert verified.status is ExecutionStatus.SUCCEEDED, verified.error
    assert verified.evidence["genus"] == 1
    assert verified.evidence["volume"] == pytest.approx(
        5.0 * (50.0 * 20.0 - _hole_area(3.5)), rel=1e-3
    )
