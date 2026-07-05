"""A model's opinion enters assembly as a prior — priced, parsed, distrusted.

The ``ProposalModel`` seam lets an LLM weigh in on producer picks, but only
as pseudo-observations on the same Beta posterior verified history feeds:
advice decides thin-history ties, evidence washes it out, and a failing
model never blocks assembly. Every call is metered, and what the advice
cost rides the preview into the same budget verdict as the market gross.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from test_gateway_market import _build, _contribute_and_publish
from test_market_assemble import RAW as RAW_SLOT
from test_market_assemble import TIDY, _seed_market

from workflow_gps.desktop import DesktopService
from workflow_gps.metering.model_calls import (
    DEFAULT_MODEL_PRICES,
    ModelCallMeter,
    ModelPriceTable,
)
from workflow_gps.models import ModelTier
from workflow_gps.orchestrator import ContractAssembler, GoalSpec, Proposal
from workflow_gps.orchestrator.proposals import (
    PROPOSAL_PURPOSE,
    PROPOSAL_SYSTEM_PROMPT,
    GatewayProposalModel,
    parse_weights,
)
from workflow_gps.routing.gateway import FakeGateway, GatewayError, SynthesisResult
from workflow_gps.skills import ActionsBody, NodeContract, NodeStats, Slot
from workflow_gps.skills.models import ActionEvent

RAW = Slot(name="raw", value_type="path")
TIDY_OUT = Slot(name="tidy", value_type="path")


def _producer(
    name, *, successes=0, failures=0, cost=None, consumes=(), produces=(RAW,)
):
    return NodeContract(
        id=f"lib.{name}",
        name=name,
        consumes=list(consumes),
        produces=list(produces),
        body=ActionsBody(
            actions=[ActionEvent(correlation_id="c", adapter="stub", operation=name)]
        ),
        stats=NodeStats(successes=successes, failures=failures, cost_ewma=cost),
    )


class _StubModel:
    """A scripted ProposalModel; records what the assembler asked."""

    def __init__(self, weights, *, cost=0.0, error=None):
        self.calls: list[tuple] = []
        self._weights = weights
        self._cost = cost
        self._error = error

    def propose(self, *, goal, slot, selected, candidates):
        self.calls.append(
            (goal.name, slot.name, list(selected), [c.id for c in candidates])
        )
        if self._error is not None:
            raise self._error
        return Proposal(weights=self._weights, cost=self._cost)


GOAL = GoalSpec(name="get-raw", want=[RAW])


# --------------------------------------------------------------------------- #
# The prior: advice decides ties, evidence outranks advice.                    #
# --------------------------------------------------------------------------- #
def test_no_model_means_todays_behavior_and_zero_planning_cost():
    result = ContractAssembler([_producer("alpha"), _producer("beta")]).assemble(GOAL)
    assert result.selected == ["alpha"]  # the stable name tie-break
    assert result.planning_cost == 0.0


def test_endorsement_decides_a_thin_history_tie():
    library = [_producer("alpha"), _producer("beta")]
    model = _StubModel({"lib.beta": 1.0}, cost=0.01)
    result = ContractAssembler(library, proposal_model=model).assemble(GOAL)
    assert result.selected == ["beta"]  # the model's prior broke the tie
    assert result.planning_cost == 0.01

    # A condemnation works the same way: advising AGAINST alpha picks beta.
    against = _StubModel({"lib.alpha": 0.0})
    result = ContractAssembler(library, proposal_model=against).assemble(GOAL)
    assert result.selected == ["beta"]


def test_verified_history_outweighs_the_models_opinion():
    proven = _producer("proven", successes=30, failures=0)
    rival = _producer("rival", failures=10)
    # The model does its worst: condemns the proven node, endorses the rival.
    model = _StubModel({"lib.proven": 0.0, "lib.rival": 1.0})
    result = ContractAssembler([proven, rival], proposal_model=model).assemble(GOAL)
    assert result.selected == ["proven"]  # evidence beats opinion


def test_a_failing_model_never_blocks_assembly():
    library = [_producer("alpha"), _producer("beta")]
    model = _StubModel({}, error=RuntimeError("model exploded"))
    result = ContractAssembler(library, proposal_model=model).assemble(GOAL)
    assert result.complete
    assert result.selected == ["alpha"]  # exactly the no-model pick
    assert result.planning_cost == 0.0  # no completion, no charge


def test_a_single_candidate_is_never_worth_a_model_call():
    model = _StubModel({"lib.only": 1.0}, cost=0.5)
    result = ContractAssembler([_producer("only")], proposal_model=model).assemble(GOAL)
    assert result.complete
    assert model.calls == []  # there was no choice to advise on
    assert result.planning_cost == 0.0


def test_unknown_ids_are_ignored_and_wild_weights_clamp():
    library = [_producer("alpha"), _producer("beta")]
    ghost = _StubModel({"lib.ghost": 1.0})
    assert (
        ContractAssembler(library, proposal_model=ghost).assemble(GOAL).selected
        == ["alpha"]  # advice about a non-candidate changes nothing
    )
    wild = _StubModel({"lib.beta": 99.0, "lib.alpha": -5.0})
    assert (
        ContractAssembler(library, proposal_model=wild).assemble(GOAL).selected
        == ["beta"]  # clamped to endorse-beta / condemn-alpha, not amplified
    )


def test_planning_cost_accumulates_across_consulted_picks():
    library = [
        _producer("tidy-a", consumes=[RAW], produces=[TIDY_OUT]),
        _producer("tidy-b", consumes=[RAW], produces=[TIDY_OUT]),
        _producer("raw-a"),
        _producer("raw-b"),
    ]
    model = _StubModel({}, cost=0.25)
    result = ContractAssembler(library, proposal_model=model).assemble(
        GoalSpec(name="clean", want=[TIDY_OUT])
    )
    assert result.complete
    assert len(model.calls) == 2  # one consultation per contested slot
    assert result.planning_cost == 0.5


def test_endorsement_shifts_thompson_exploration():
    library = [_producer("alpha"), _producer("beta")]
    rng = random.Random(7)
    model = _StubModel({"lib.beta": 1.0})
    picks = [
        ContractAssembler(
            library, rng=rng, proposal_model=model, proposal_strength=50.0
        )
        .assemble(GOAL)
        .selected[0]
        for _ in range(20)
    ]
    # With no history at all the two are 50/50; a strong endorsement makes
    # exploration overwhelmingly (not exclusively) favor the endorsed node.
    assert picks.count("beta") >= 16


# --------------------------------------------------------------------------- #
# Parsing: unreadable advice is no advice.                                     #
# --------------------------------------------------------------------------- #
def test_parse_weights_accepts_bare_wrapped_fenced_and_embedded_json():
    assert parse_weights('{"a": 0.9, "b": 0.1}') == {"a": 0.9, "b": 0.1}
    assert parse_weights('{"weights": {"a": 1}}') == {"a": 1.0}
    fenced = 'Sure!\n```json\n{"a": 0.7}\n```\nHope that helps.'
    assert parse_weights(fenced) == {"a": 0.7}
    embedded = 'I would weigh it as {"a": 0.5} overall.'
    assert parse_weights(embedded) == {"a": 0.5}


def test_parse_weights_clamps_and_drops_junk():
    assert parse_weights('{"a": 7, "b": -3}') == {"a": 1.0, "b": 0.0}
    assert parse_weights('{"a": true, "b": "high"}') == {}
    assert parse_weights("no idea") == {}
    assert parse_weights("") == {}
    assert parse_weights(None) == {}
    assert parse_weights("[1, 2, 3]") == {}


# --------------------------------------------------------------------------- #
# Metering: model calls are never free.                                        #
# --------------------------------------------------------------------------- #
def _completion(*, tier=ModelTier.FAST, prompt_tokens=0, completion_tokens=0):
    return SynthesisResult(
        raw_text="",
        script=None,
        model="m",
        tier=tier,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def test_meter_prices_tokens_by_tier():
    meter = ModelCallMeter(clock=lambda: datetime(2026, 6, 29, tzinfo=UTC))
    record = meter.record(
        "assembly.proposal",
        _completion(prompt_tokens=1_000_000, completion_tokens=1_000_000),
    )
    # Fast tier at the default table: 0.10 + 0.30 per million.
    assert record.cost == 0.10 + 0.30
    assert record.tier == "fast"
    assert record.at == datetime(2026, 6, 29, tzinfo=UTC)


def test_unknown_tiers_are_priced_conservatively_never_free():
    table = DEFAULT_MODEL_PRICES
    assert table.cost("mystery", 1_000_000, 0) == table.default_prompt_per_million
    assert table.cost("mystery", 1_000_000, 0) >= table.cost("fast", 1_000_000, 0)


def test_meter_filters_by_purpose_and_totals():
    meter = ModelCallMeter(
        prices=ModelPriceTable(
            prompt_per_million={"fast": 1.0}, completion_per_million={"fast": 2.0}
        )
    )
    meter.record("assembly.proposal", _completion(prompt_tokens=500_000))
    meter.record("synthesis", _completion(completion_tokens=250_000))
    assert meter.total_cost("assembly.proposal") == 0.5
    assert meter.total_cost("synthesis") == 0.5
    assert meter.total_cost() == 1.0
    assert [r.purpose for r in meter.charges("synthesis")] == ["synthesis"]


# --------------------------------------------------------------------------- #
# The gateway-backed model: prompt shape, shortlist, parsing, pricing.         #
# --------------------------------------------------------------------------- #
class _TokenGateway:
    """A Gateway whose completions carry token telemetry, unlike FakeGateway."""

    def __init__(self, text, *, prompt_tokens, completion_tokens):
        self._text = text
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens

    @property
    def name(self) -> str:
        return "tokens"

    def complete(self, decision, prompt):
        return SynthesisResult(
            raw_text=self._text,
            script=None,
            model=decision.model,
            tier=decision.tier,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            finish_reason="stop",
        )

    def close(self) -> None:
        return None


def test_gateway_proposal_parses_weights_and_meters_the_call():
    gateway = FakeGateway(['```json\n{"lib.beta": 0.9}\n```'])
    model = GatewayProposalModel(gateway)
    proposal = model.propose(
        goal=GOAL,
        slot=RAW,
        selected=[],
        candidates=[_producer("alpha"), _producer("beta")],
    )
    assert proposal.weights == {"lib.beta": 0.9}
    (charge,) = model.meter.charges(PROPOSAL_PURPOSE)
    assert charge.model == gateway.calls[0][0].model
    # FakeGateway reports no token usage, so the charge is honestly zero.
    assert proposal.cost == charge.cost == 0.0

    decision, prompt = gateway.calls[0]
    assert decision.tier is ModelTier.FAST
    assert decision.max_tokens == 512  # ranking is a small job by construction
    # The system message is the frozen, cacheable prefix.
    assert prompt.prefix_len == 1
    assert prompt.messages[0]["content"] == PROPOSAL_SYSTEM_PROMPT
    assert "lib.alpha" in prompt.messages[1]["content"]
    assert "lib.beta" in prompt.messages[1]["content"]


def test_gateway_proposal_shortlists_the_strongest_candidates():
    gateway = FakeGateway(["{}"])
    model = GatewayProposalModel(gateway, max_candidates=2)
    candidates = [
        _producer("weak", failures=10),
        _producer("strong", successes=10),
        _producer("middling"),
    ]
    model.propose(goal=GOAL, slot=RAW, selected=[], candidates=candidates)
    body = gateway.calls[0][1].messages[1]["content"]
    assert "lib.strong" in body and "lib.middling" in body
    assert "lib.weak" not in body  # the long tail stays neutral, not prompted


def test_gateway_proposal_prices_real_token_usage_into_the_plan():
    gateway = _TokenGateway(
        '{"lib.beta": 1.0}', prompt_tokens=500_000, completion_tokens=250_000
    )
    meter = ModelCallMeter(
        prices=ModelPriceTable(
            prompt_per_million={"fast": 1.0}, completion_per_million={"fast": 2.0}
        )
    )
    model = GatewayProposalModel(gateway, meter=meter)
    result = ContractAssembler(
        [_producer("alpha"), _producer("beta")], proposal_model=model
    ).assemble(GOAL)
    assert result.selected == ["beta"]
    assert result.planning_cost == (500_000 * 1.0 + 250_000 * 2.0) / 1_000_000
    assert meter.total_cost(PROPOSAL_PURPOSE) == result.planning_cost


def test_a_dead_endpoint_downgrades_to_verified_history_assembly():
    gateway = FakeGateway([GatewayError("endpoint down")])
    model = GatewayProposalModel(gateway)
    result = ContractAssembler(
        [_producer("alpha"), _producer("beta")], proposal_model=model
    ).assemble(GOAL)
    assert result.complete
    assert result.selected == ["alpha"]  # the no-model pick
    assert result.planning_cost == 0.0


def test_unreadable_advice_is_no_advice():
    gateway = FakeGateway(["I really cannot decide between these."])
    model = GatewayProposalModel(gateway)
    proposal = model.propose(
        goal=GOAL, slot=RAW, selected=[], candidates=[_producer("a"), _producer("b")]
    )
    assert proposal.weights == {}


# --------------------------------------------------------------------------- #
# Surfaces: planning cost rides the preview and the budget judges the sum.     #
# --------------------------------------------------------------------------- #
def _desktop_with_market(tmp_path, *, proposal_model=None):
    app, conn, ident, registry, *_rest = _build(tmp_path)
    _seed_market(app, ident, registry)
    # A second raw producer so the RAW pick is contested and worth advising.
    _contribute_and_publish(
        app,
        ident,
        registry,
        name="raw exporter deluxe",
        noder="noder-deluxe",
        price=0.10,
        produces=[RAW_SLOT],
        consumes=[],
    )
    svc = DesktopService(
        app._durable,
        market=app._market,
        price_book=app._price_book,
        proposal_model=proposal_model,
    )
    return app, svc, conn


def test_desktop_preview_surfaces_planning_cost_and_budgets_judge_the_sum(tmp_path):
    model = _StubModel({}, cost=0.05)
    _app, svc, conn = _desktop_with_market(tmp_path, proposal_model=model)

    view = svc.assembly_preview(goal="clean-the-books", want=[TIDY])
    assert view.complete is True
    assert model.calls, "the contested RAW pick should have been advised"
    assert view.planning_cost == 0.05

    # A threshold between the market gross and gross+planning: only the
    # planning charge tips it over, proving budgets judge the whole cost.
    threshold = view.estimated_gross_total + 0.01
    judged = svc.assembly_preview(
        goal="clean-the-books", want=[TIDY], review_threshold=threshold
    )
    assert judged.budget is not None
    assert judged.budget["estimated"] == (
        judged.estimated_gross_total + judged.planning_cost
    )
    assert judged.budget["needs_review"] is True
    conn.close()


def test_gateway_market_assemble_reports_planning_cost(tmp_path):
    from test_http_gateway import _req

    app, conn, ident, registry, *_rest = _build(tmp_path)
    _seed_market(app, ident, registry)
    _contribute_and_publish(
        app,
        ident,
        registry,
        name="raw exporter deluxe",
        noder="noder-deluxe",
        price=0.10,
        produces=[RAW_SLOT],
        consumes=[],
    )
    app._proposal_model = _StubModel({}, cost=0.03)

    response = app.handle(
        _req(
            "POST",
            "/v1/market/assemble",
            token=ident.token("consumer", "t2"),
            body={"goal": {"name": "clean-the-books", "want": [TIDY]}},
        )
    )
    assert response.status == 200, response.body
    assert response.body["planning_cost"] == 0.03
    assert response.body["budget"]["estimated"] == (
        response.body["estimated_gross_total"] + 0.03
    )
    conn.close()
