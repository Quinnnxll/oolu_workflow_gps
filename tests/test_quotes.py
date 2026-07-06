"""Consumer quoting: coverage, warnings, mode choice, previews vs payouts."""

from __future__ import annotations

import pytest

from oolu.nodeplace import (
    CandidateEconomics,
    ConsumerAccount,
    CostVector,
    Coverage,
    NodeClass,
    PriceBook,
    QuoteEngine,
    QuoteMode,
    RewardSignals,
    StepCandidates,
    SubscriptionPlan,
)

PLAN = SubscriptionPlan(
    name="Professional Automation",
    monthly_price=200.0,
    automation_cost_budget=65.0,
    included_cli_calls=100,
    included_api_calls=50,
)


def _account(**kwargs) -> ConsumerAccount:
    defaults = dict(user_id="u1", plan=PLAN)
    defaults.update(kwargs)
    return ConsumerAccount(**defaults)


def _candidate(version_id: str, **kwargs) -> CandidateEconomics:
    defaults = dict(
        version_id=version_id,
        noder_principal=f"noder-{version_id}",
        node_class=NodeClass.WORKFLOW,
        class_key=f"wf:{version_id}",
        cleared_price=0.20,
        cost=CostVector(model=0.02),
        verified_successes=40,
        verified_failures=2,
        latency_seconds=3.0,
    )
    defaults.update(kwargs)
    return CandidateEconomics(**defaults)


def _steps() -> list[StepCandidates]:
    convert = _candidate(
        "convert",
        node_class=NodeClass.COMMODITY,
        class_key="commodity:convert",
        cleared_price=0.01,
        cost=CostVector(cli=0.001),
    )
    clean = _candidate("clean", cleared_price=0.18)
    filing = _candidate(
        "filing",
        node_class=NodeClass.REGULATED,
        class_key="gov:file",
        cleared_price=18.0,
        cost=CostVector(api=0.05),
    )
    review = _candidate(
        "review",
        node_class=NodeClass.PROFESSIONAL,
        class_key="professional:review",
        cleared_price=75.0,
        cost=CostVector(verification=0.2),
        difficulty=4.0,
        scarcity=2.8,
    )
    return [
        StepCandidates(name="Convert CSVs", candidates=[convert], cli_calls=1),
        StepCandidates(name="Clean invoices", candidates=[clean], api_calls=1),
        StepCandidates(
            name="File with the portal",
            candidates=[filing],
            api_calls=1,
            vendor="Official Tax Portal",
        ),
        StepCandidates(name="Professional review", candidates=[review]),
    ]


def test_coverage_splits_plan_lines_from_pass_through_invoices():
    quote = QuoteEngine(PriceBook(":memory:")).quote(_account(), _steps())
    by_label = {line.label: line for line in quote.invoice_lines}
    assert by_label["Included automation: Convert CSVs"].amount == 0.0
    assert by_label["Included automation: Clean invoices"].amount == 0.0
    outside = [
        line for line in quote.invoice_lines if line.coverage is Coverage.OUTSIDE_PLAN
    ]
    assert {line.vendor for line in outside} == {
        "Official Tax Portal",
        "third-party provider",
    }
    # The user pays exactly the outside-plan lines, nothing hidden.
    assert quote.total_user_due_now == pytest.approx(
        sum(line.amount for line in outside)
    )
    assert quote.subscription_covered_value > 0


def test_warnings_accumulate_instead_of_overwriting():
    tight_plan = PLAN.model_copy(
        update={
            "automation_cost_budget": 0.001,
            "included_cli_calls": 0,
            "included_api_calls": 0,
        }
    )
    quote = QuoteEngine(PriceBook(":memory:")).quote(
        _account(plan=tight_plan), _steps()
    )
    assert len(quote.warnings) == 3  # budget + CLI quota + API quota, all reported


def test_mode_changes_the_chosen_candidate():
    cheap = _candidate(
        "cheap",
        class_key="wf:clean",
        cleared_price=0.05,
        verified_successes=10,
        verified_failures=5,
    )
    proven = _candidate(
        "proven",
        class_key="wf:clean",
        cleared_price=0.60,
        verified_successes=400,
        verified_failures=2,
        reputation=1.9,
    )
    step = StepCandidates(name="Clean", candidates=[cheap, proven])
    engine = QuoteEngine(PriceBook(":memory:"))
    budget = engine.quote(_account(), [step], mode=QuoteMode.BUDGET)
    certified = engine.quote(_account(), [step], mode=QuoteMode.CERTIFIED)
    assert budget.steps[0].chosen.version_id == "cheap"
    assert certified.steps[0].chosen.version_id == "proven"


def test_expected_cost_is_retry_adjusted():
    flaky = _candidate(
        "flaky", verified_successes=5, verified_failures=5, cost=CostVector(model=0.10)
    )
    steady = _candidate(
        "steady",
        verified_successes=98,
        verified_failures=0,
        cost=CostVector(model=0.10),
    )
    engine = QuoteEngine(PriceBook(":memory:"))
    flaky_quote = engine.quote(
        _account(), [StepCandidates(name="s", candidates=[flaky])]
    )
    steady_quote = engine.quote(
        _account(), [StepCandidates(name="s", candidates=[steady])]
    )
    assert flaky_quote.expected_automation_cost > steady_quote.expected_automation_cost


def test_payout_previews_are_labeled_forecasts_and_regulated_has_none():
    steps = _steps()
    signals = {
        "clean": RewardSignals(
            node_class=NodeClass.WORKFLOW, reputation=1.5, verified_successes=40
        )
    }
    steps[1] = steps[1].model_copy(update={"signals": signals})
    quote = QuoteEngine(PriceBook(":memory:")).quote(_account(), steps)

    assert quote.payout_previews, "covered + professional steps must preview payouts"
    assert all("verified success" in p.reason for p in quote.payout_previews)
    regulated_step = next(s for s in quote.steps if s.step == "File with the portal")
    assert regulated_step.payout_previews == []  # pass-through: no pool
    assert regulated_step.platform_margin_preview == 0.0
    # Preview economics stay consistent: noders + platform <= cleared price.
    clean_step = next(s for s in quote.steps if s.step == "Clean invoices")
    total_split = (
        sum(p.amount for p in clean_step.payout_previews)
        + clean_step.platform_margin_preview
    )
    assert total_split <= clean_step.cleared.cleared + 1e-9


def test_settle_usage_charges_the_plan_counters():
    engine = QuoteEngine(PriceBook(":memory:"))
    steps = _steps()
    account = _account()
    quote = engine.quote(account, steps)
    settled = engine.settle_usage(account, quote, steps)
    assert settled.used_automation_budget == pytest.approx(
        quote.expected_automation_cost
    )
    assert settled.used_cli_calls == 1
    assert settled.used_api_calls == 2


def test_missing_candidates_fail_loudly():
    with pytest.raises(ValueError, match="no candidates"):
        QuoteEngine(PriceBook(":memory:")).quote(
            _account(), [StepCandidates(name="empty", candidates=[])]
        )
