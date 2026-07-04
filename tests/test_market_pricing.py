"""Price formation: floors, competition, value anchor, damping, ranking."""

from __future__ import annotations

from workflow_gps.nodeplace import (
    CandidateEconomics,
    CostVector,
    NodeClass,
    PriceBook,
    QuoteMode,
    competition_index,
    estimate_user_value,
    rank_candidates,
    utility,
)

COST = CostVector(cli=0.01, compute=0.01)  # automation cost 0.02


def _clear(book: PriceBook, ask: float, **kwargs):
    defaults = dict(
        class_key="commodity:convert",
        node_class=NodeClass.COMMODITY,
        ask=ask,
        cost=COST,
    )
    defaults.update(kwargs)
    return book.clear(**defaults)


def test_price_never_clears_below_cost_floor():
    book = PriceBook(":memory:")
    cleared = _clear(book, ask=0.001)  # asks below cost
    assert cleared.cleared >= cleared.cost_floor > COST.automation_cost
    assert "cost floor engaged" in cleared.notes


def test_competition_pulls_commodities_hard_and_professionals_barely():
    commodity_book = PriceBook(":memory:")
    crowded = _clear(commodity_book, ask=1.0, substitutes=8)
    lonely = _clear(PriceBook(":memory:"), ask=1.0, substitutes=0)
    assert crowded.cleared < lonely.cleared  # substitutes push the price down

    pro_book = PriceBook(":memory:")
    pro = pro_book.clear(
        class_key="professional:review",
        node_class=NodeClass.PROFESSIONAL,
        ask=100.0,
        cost=CostVector(verification=0.2),
        substitutes=8,
    )
    # Professional pricing keeps power: an 8-substitute pull barely moves it.
    assert pro.cleared > 95.0


def test_value_anchor_caps_the_price():
    book = PriceBook(":memory:")
    value = estimate_user_value(minutes_saved=10, hourly_rate=60.0)  # 10.0
    cleared = _clear(book, ask=50.0, user_value=value)
    assert cleared.cleared <= value * 0.35 + 1e-9
    assert "value anchor engaged" in cleared.notes


def test_damping_band_limits_movement_per_period():
    book = PriceBook(":memory:")
    first = _clear(book, ask=1.0)  # establishes the reference
    jumped = _clear(book, ask=10.0, days_elapsed=30.0)  # 10x ask next month
    # Commodity band allows at most +8% per 30-day period.
    assert jumped.cleared <= first.cleared * 1.08 + 1e-9
    assert "damping band engaged" in jumped.notes


def test_regulated_fees_pass_through_untouched():
    book = PriceBook(":memory:")
    cleared = book.clear(
        class_key="gov:tax-filing",
        node_class=NodeClass.REGULATED,
        ask=18.0,
        cost=CostVector(api=0.05),
        substitutes=50,
        user_value=1.0,  # even a tiny value anchor must not touch it
    )
    assert cleared.cleared == 18.0
    assert book.reference("gov:tax-filing") is None  # not even tracked


def test_reference_prices_persist_across_reopen(tmp_path):
    path = tmp_path / "prices.db"
    book = PriceBook(path)
    first = _clear(book, ask=1.0)
    book.close()
    reopened = PriceBook(path)
    assert reopened.reference("commodity:convert") == first.cleared
    reopened.close()


def test_competition_index_saturates():
    assert competition_index(0) == 0.0
    assert competition_index(4) == 0.5
    assert 0.8 < competition_index(50) < 1.0
    # Substitutes that are not quality-comparable barely count.
    assert competition_index(8, quality_parity=0.1) < competition_index(2)


# --------------------------------------------------------------------------- #
# Ranking: verified quality per retry-adjusted dollar.                         #
# --------------------------------------------------------------------------- #
def _candidate(version_id: str, **kwargs) -> CandidateEconomics:
    defaults = dict(
        version_id=version_id,
        noder_principal=f"noder-{version_id}",
        node_class=NodeClass.WORKFLOW,
        class_key="wf:clean",
        cleared_price=0.20,
        verified_successes=50,
        verified_failures=2,
        reputation=1.0,
        latency_seconds=5.0,
    )
    defaults.update(kwargs)
    return CandidateEconomics(**defaults)


def test_budget_mode_prefers_cheap_certified_prefers_proven_quality():
    cheap_ok = _candidate(
        "cheap", cleared_price=0.05, verified_successes=20, verified_failures=4
    )
    pricey_proven = _candidate(
        "proven",
        cleared_price=0.40,
        verified_successes=500,
        verified_failures=3,
        reputation=1.8,
    )
    budget = rank_candidates([cheap_ok, pricey_proven], QuoteMode.BUDGET)
    certified = rank_candidates([cheap_ok, pricey_proven], QuoteMode.CERTIFIED)
    assert budget[0].version_id == "cheap"
    assert certified[0].version_id == "proven"


def test_unreliable_nodes_pay_a_retry_penalty():
    flaky = _candidate(
        "flaky", cleared_price=0.10, verified_successes=5, verified_failures=15
    )
    steady = _candidate(
        "steady", cleared_price=0.10, verified_successes=15, verified_failures=5
    )
    assert flaky.effective_price > steady.effective_price
    assert utility(steady, QuoteMode.STANDARD) > utility(flaky, QuoteMode.STANDARD)


def test_ranking_uses_verified_stats_not_self_declared_numbers():
    # A brand-new node cannot buy rank with claims: with no verified runs it
    # sits at the neutral 0.5 posterior regardless of anything it asserts.
    newcomer = _candidate("new", verified_successes=0, verified_failures=0)
    assert newcomer.success_mean == 0.5
    veteran = _candidate("vet", verified_successes=200, verified_failures=2)
    assert utility(veteran, QuoteMode.STANDARD) > utility(newcomer, QuoteMode.STANDARD)
