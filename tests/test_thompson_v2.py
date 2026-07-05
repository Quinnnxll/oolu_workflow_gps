"""Thompson v2: recency decay, cost-aware utility, plan confidence, replay.

Four upgrades to the learning loop, each pinned here:
- discounted posteriors (``TraceStore(recency_decay=...)``) track a node's
  recent self, so a regressed node stops looking as good as ever;
- ``cost_weight`` ranks picks by expected utility instead of quality alone,
  so a slightly-less-proven cheap node can honestly beat a proven dear one;
- previews carry ``expected_success`` — the plan's chance of verified
  success in the caller's own hands;
- the offline replay harness auditions strategies on seeded worlds (fitted
  from recorded history if desired) before any change touches real money.
"""

from __future__ import annotations

import random

import pytest
from test_gateway_market import _build, _contribute_and_publish
from test_http_gateway import _req
from test_market_assemble import RAW as RAW_SLOT
from test_market_assemble import TIDY, _seed_market

from workflow_gps.desktop import DesktopService
from workflow_gps.knowledge import NodeObservation, TraceStore, route_node_key
from workflow_gps.knowledge.replay import (
    Arm,
    PosteriorStrategy,
    ReplayWorld,
    evaluate,
)
from workflow_gps.orchestrator import ContractAssembler, GoalSpec
from workflow_gps.skills import ActionsBody, NodeContract, NodeStats, Slot
from workflow_gps.skills.models import ActionEvent

RAW = Slot(name="raw", value_type="path")


def _producer(name, *, successes=0, failures=0, cost=None):
    return NodeContract(
        id=f"lib.{name}",
        name=name,
        produces=[RAW],
        body=ActionsBody(
            actions=[ActionEvent(correlation_id="c", adapter="stub", operation=name)]
        ),
        stats=NodeStats(successes=successes, failures=failures, cost_ewma=cost),
    )


def _observe(store, key, ok, *, cost=None, context=""):
    store.record_run(
        goal="g",
        steps=[NodeObservation(key, ok=ok, cost=cost)],
        success=ok,
        context=context,
    )


# --------------------------------------------------------------------------- #
# Recency decay: the posterior tracks the node's recent self.                  #
# --------------------------------------------------------------------------- #
def test_decay_discounts_old_observations_exactly():
    store = TraceStore(recency_decay=0.5)
    for ok in (True, True, False, False, False):
        _observe(store, "n", ok)
    post = store.posterior("n")
    # S,S,F,F,F at decay .5: s = ((1*.5+1)*.5)*.5*.5 = 0.1875, f = 1.75.
    assert post.successes == pytest.approx(0.1875)
    assert post.failures == pytest.approx(1.75)
    store.close()

    flat = TraceStore()  # decay 1.0: exact integer counting, unchanged
    for ok in (True, True, False, False, False):
        _observe(flat, "n", ok)
    post = flat.posterior("n")
    assert (post.successes, post.failures) == (2, 3)
    flat.close()


def test_a_regressed_node_stops_looking_as_good_as_ever():
    history = [True] * 10 + [False] * 5  # ten old wins, five fresh losses
    decayed, flat = TraceStore(recency_decay=0.8), TraceStore()
    for ok in history:
        _observe(decayed, "n", ok)
        _observe(flat, "n", ok)
    # Flat counting still credits the old glory; the decayed posterior has
    # let it fade and now reads the node as more likely bad than good.
    assert flat.posterior("n").mean > 0.6
    assert decayed.posterior("n").mean < 0.4
    decayed.close()
    flat.close()


def test_decay_must_be_a_sane_fraction():
    with pytest.raises(ValueError):
        TraceStore(recency_decay=0.0)
    with pytest.raises(ValueError):
        TraceStore(recency_decay=1.5)


# --------------------------------------------------------------------------- #
# Cost-aware acquisition: rank by utility, not quality alone.                  #
# --------------------------------------------------------------------------- #
def test_cost_weight_lets_a_cheap_node_beat_a_proven_dear_one():
    premium = _producer("premium", successes=99, failures=1, cost=10.0)
    econo = _producer("econo", successes=97, failures=3, cost=1.0)
    goal = GoalSpec(name="get-raw", want=[RAW])

    # Weight 0 (the default): quality rules, cost stays a tie-break.
    assert ContractAssembler([premium, econo]).assemble(goal).selected == ["premium"]
    # A declared trade: each dollar is worth 5 points of success. The
    # premium node's edge (~2 points) cannot justify 9 dollars.
    picked = ContractAssembler([premium, econo], cost_weight=0.05).assemble(goal)
    assert picked.selected == ["econo"]


def test_cost_weight_shapes_thompson_exploration_too():
    premium = _producer("premium", successes=90, failures=10, cost=10.0)
    econo = _producer("econo", successes=85, failures=15, cost=1.0)
    goal = GoalSpec(name="get-raw", want=[RAW])
    rng = random.Random(11)
    picks = [
        ContractAssembler([premium, econo], rng=rng, cost_weight=0.05)
        .assemble(goal)
        .selected[0]
        for _ in range(20)
    ]
    assert picks.count("econo") >= 16


def test_negative_cost_weight_is_refused():
    with pytest.raises(ValueError):
        ContractAssembler([], cost_weight=-0.1)


# --------------------------------------------------------------------------- #
# The replay harness: strategies audition before they ship.                    #
# --------------------------------------------------------------------------- #
def test_thompson_finds_and_keeps_the_best_arm():
    world = ReplayWorld({"best": Arm(0.9, cost=1.0), "decoy": Arm(0.3, cost=1.0)})
    (report,) = evaluate(
        [(world, 300)], [PosteriorStrategy("thompson")], seed=42
    ).values()
    assert report.rounds == 300
    assert report.picks["best"] > report.picks["decoy"] * 3
    assert report.success_rate > 0.75
    assert report.regret < 0.2 * report.oracle_successes


def test_recency_decay_adapts_to_drift_faster():
    before = ReplayWorld({"a": Arm(0.9), "b": Arm(0.55)})
    after = ReplayWorld({"a": Arm(0.05), "b": Arm(0.55)})  # a collapses
    reports = evaluate(
        [(before, 200), (after, 200)],
        [
            PosteriorStrategy("flat"),
            PosteriorStrategy("decayed", recency_decay=0.9),
        ],
        seed=7,
    )
    # Flat counting keeps trusting arm a's 180 old wins deep into the
    # collapse; the decayed posterior lets them fade and switches to b.
    assert reports["decayed"].successes > reports["flat"].successes
    assert reports["decayed"].regret < reports["flat"].regret


def test_cost_aware_strategy_buys_success_cheaper():
    world = ReplayWorld({"premium": Arm(0.95, cost=10.0), "econo": Arm(0.90, cost=1.0)})
    reports = evaluate(
        [(world, 300)],
        [
            PosteriorStrategy("quality-only"),
            PosteriorStrategy("cost-aware", cost_weight=0.05),
        ],
        seed=3,
    )
    aware, blind = reports["cost-aware"], reports["quality-only"]
    assert aware.picks.get("econo", 0) > aware.picks.get("premium", 0)
    assert aware.spend < blind.spend
    # The success given up for that saving is small — rates sit 5 points apart.
    assert aware.success_rate > 0.8


def test_worlds_can_be_fitted_from_recorded_history():
    store = TraceStore()
    for ok in (True, True, True, False):
        _observe(store, "steady", ok, cost=2.0)
    world = ReplayWorld.from_trace_store(store, ["steady", "unknown"])
    assert world.arm("steady").success_rate == store.posterior("steady").mean
    assert world.arm("steady").cost == pytest.approx(2.0)
    # An arm with no history behaves like its uniform prior, free to run.
    assert world.arm("unknown").success_rate == 0.5
    assert world.arm("unknown").cost == 0.0
    store.close()


def test_replay_is_deterministic_under_a_seed():
    world = ReplayWorld({"a": Arm(0.7), "b": Arm(0.5)})
    first = evaluate([(world, 50)], [PosteriorStrategy("t")], seed=9)["t"]
    second = evaluate([(world, 50)], [PosteriorStrategy("t")], seed=9)["t"]
    assert (first.successes, first.spend, first.picks) == (
        second.successes,
        second.spend,
        second.picks,
    )


# --------------------------------------------------------------------------- #
# Surfaces: expected_success and cost_weight ride the previews.                 #
# --------------------------------------------------------------------------- #
def _desktop_with_market(tmp_path, *, trace_store=None, second_raw_producer=False):
    app, conn, ident, registry, *_rest = _build(tmp_path)
    _seed_market(app, ident, registry)
    if second_raw_producer:
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
        trace_store=trace_store,
    )
    return app, svc, conn, ident


def test_preview_reports_the_plans_expected_success(tmp_path):
    store = TraceStore()
    _app, svc, conn, _ident = _desktop_with_market(tmp_path, trace_store=store)

    fresh = svc.assembly_preview(goal="clean-the-books", want=[TIDY])
    # Two picked nodes, no history anywhere: 0.5 * 0.5.
    assert fresh.expected_success == pytest.approx(0.25)

    for name in ("raw exporter", "invoice cleaner"):
        for _ in range(3):
            _observe(store, route_node_key(name), True)
    proven = svc.assembly_preview(goal="clean-the-books", want=[TIDY])
    # Three personal wins each: posterior mean 4/5 per node.
    assert proven.expected_success == pytest.approx(0.8 * 0.8)
    assert proven.expected_success > fresh.expected_success

    impossible = svc.assembly_preview(
        goal="impossible", want=[{"name": "unicorn", "value_type": "path"}]
    )
    assert impossible.expected_success is None  # nothing assembled, no claim
    store.close()
    conn.close()


def test_cost_weight_flips_the_desktop_pick_by_the_declared_trade(tmp_path):
    store = TraceStore()
    # Personal history: the original exporter is more proven but dear;
    # the deluxe one is a little shakier and a tenth the price.
    for ok, cost in ((True, 5.0), (True, 5.0), (True, 5.0), (True, 5.0)):
        _observe(store, route_node_key("raw exporter"), ok, cost=cost)
    for ok, cost in ((True, 0.5), (True, 0.5), (False, 0.5)):
        _observe(store, route_node_key("raw exporter deluxe"), ok, cost=cost)
    _app, svc, conn, _ident = _desktop_with_market(
        tmp_path, trace_store=store, second_raw_producer=True
    )

    quality = svc.assembly_preview(goal="clean-the-books", want=[TIDY])
    assert "raw exporter" in quality.selected
    assert "raw exporter deluxe" not in quality.selected

    thrifty = svc.assembly_preview(goal="clean-the-books", want=[TIDY], cost_weight=0.1)
    assert "raw exporter deluxe" in thrifty.selected
    store.close()
    conn.close()


def test_gateway_assemble_validates_and_accepts_cost_weight(tmp_path):
    app, conn, ident, registry, *_rest = _build(tmp_path)
    _seed_market(app, ident, registry)

    def assemble(body):
        return app.handle(
            _req(
                "POST",
                "/v1/market/assemble",
                token=ident.token("consumer", "t2"),
                body=body,
            )
        )

    goal = {"name": "clean-the-books", "want": [TIDY]}
    ok = assemble({"goal": goal, "cost_weight": 0.2})
    assert ok.status == 200, ok.body
    assert 0.0 < ok.body["expected_success"] <= 1.0

    refused = assemble({"goal": goal, "cost_weight": -1})
    assert refused.status == 400
    garbled = assemble({"goal": goal, "cost_weight": "cheap please"})
    assert garbled.status == 400
    conn.close()
