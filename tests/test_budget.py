"""Cost-aware assembly budgets: caps, review thresholds, learned comfort.

Three signals with three authorities: a hard cap refuses and nothing
overrides it; a review threshold blocks until acknowledged; and the user's
own committed spending sets a comfort ceiling that flags outliers even when
no threshold was declared. The linked wallet is deliberately the weakest
signal — it may be a slice of the user's true assets, so it never caps,
it only asks for a look.
"""

from __future__ import annotations

import pytest
from test_contract_run import _assembled_contract, _build, _CliExecutor, _seed_chain
from test_http_gateway import _req
from test_market_assemble import TIDY

from workflow_gps.metering.models import RunBinding
from workflow_gps.nodeplace import (
    BudgetExceededError,
    BudgetPolicy,
    ReviewRequiredError,
    SpendingProfile,
    assess_budget,
    enforce_budget,
)


# --------------------------------------------------------------------------- #
# The pure budget logic.                                                       #
# --------------------------------------------------------------------------- #
def test_profile_needs_history_before_it_judges():
    assert SpendingProfile.from_history([]).comfort_ceiling is None
    assert SpendingProfile.from_history([1.0, 1.0]).comfort_ceiling is None
    profile = SpendingProfile.from_history([1.0, 1.0, 1.0])
    assert profile.typical == 1.0 and profile.comfort_ceiling == 2.0


def test_growth_within_demonstrated_habit_passes_free():
    # One big past run raises the ceiling: the user has shown they do this.
    profile = SpendingProfile.from_history([1.0, 1.0, 10.0])
    assert profile.comfort_ceiling == 10.0
    verdict = assess_budget(9.0, spend_history=[1.0, 1.0, 10.0])
    assert verdict.allowed and not verdict.needs_review


def test_hard_cap_refuses_and_acknowledgement_never_overrides():
    verdict = assess_budget(5.0, policy=BudgetPolicy(hard_cap=2.0))
    assert verdict.allowed is False
    with pytest.raises(BudgetExceededError):
        enforce_budget(verdict, review_acknowledged=True)


def test_review_threshold_blocks_until_acknowledged():
    verdict = assess_budget(5.0, policy=BudgetPolicy(review_threshold=2.0))
    assert verdict.allowed and verdict.needs_review
    with pytest.raises(ReviewRequiredError):
        enforce_budget(verdict)
    enforce_budget(verdict, review_acknowledged=True)  # explicit look: fine


def test_behavior_flags_outliers_without_any_declared_threshold():
    history = [0.5, 0.6, 0.4]
    assert not assess_budget(1.0, spend_history=history).needs_review
    verdict = assess_budget(3.0, spend_history=history)
    assert verdict.needs_review
    assert any("usual spending" in r for r in verdict.reasons)


def test_wallet_asks_for_review_but_never_caps():
    # Above the linked balance: review, because the wallet may be partial.
    poor_wallet = assess_budget(5.0, wallet_balance=1.0)
    assert poor_wallet.allowed is True and poor_wallet.needs_review
    assert any("may be partial" in r for r in poor_wallet.reasons)
    # A fat wallet grants nothing: the hard cap still refuses.
    fat_wallet = assess_budget(
        5.0, policy=BudgetPolicy(hard_cap=2.0), wallet_balance=1_000_000.0
    )
    assert fat_wallet.allowed is False


def test_reasons_accumulate_across_all_signals():
    verdict = assess_budget(
        10.0,
        policy=BudgetPolicy(hard_cap=1.0, review_threshold=0.5),
        spend_history=[0.1, 0.1, 0.1],
        wallet_balance=0.2,
    )
    assert verdict.allowed is False and verdict.needs_review
    assert len(verdict.reasons) == 4  # cap, threshold, behavior, wallet


# --------------------------------------------------------------------------- #
# Gateway enforcement on the run path; verdicts on the preview.                #
# --------------------------------------------------------------------------- #
def _spend(attribution, gross, count):
    for index in range(count):
        attribution.bind(
            RunBinding(
                run_id=f"hist-{gross}-{index}",
                version_id="v-past",
                consumer_tenant="t2",
                consumer_principal="consumer",
                gross=gross,
            )
        )


def test_assemble_preview_carries_the_budget_verdict(tmp_path):
    app, conn, ident, registry, *_rest = _build(tmp_path)
    _seed_chain(app, ident, registry)
    resp = app.handle(
        _req(
            "POST",
            "/v1/market/assemble",
            token=ident.token("consumer", "t2"),
            body={
                "goal": {"name": "clean-the-books", "want": [TIDY]},
                "q": "invoice",
                "budget": {"hard_cap": 0.01},
            },
        )
    )
    assert resp.status == 200  # a preview informs; it never blocks
    budget = resp.body["budget"]
    assert budget["estimated"] == resp.body["estimated_gross_total"]
    assert budget["allowed"] is False
    assert "hard cap" in budget["reasons"][0]
    conn.close()


def test_run_refuses_over_cap_and_gates_reviews(tmp_path):
    app, conn, ident, registry, metering, attribution, audit = _build(
        tmp_path, executors={"cli": _CliExecutor()}
    )
    _seed_chain(app, ident, registry)
    contract = _assembled_contract(app, ident)
    token = ident.token("consumer", "t2")

    def run(body_extra):
        return app.handle(
            _req(
                "POST",
                "/v1/runs/contract",
                token=token,
                body={"contract": contract, **body_extra},
            )
        )

    # Hard cap: refused before anything commits — the market never moved.
    capped = run({"budget": {"hard_cap": 0.01}})
    assert capped.status == 402
    assert capped.body["error"]["code"] == "budget_exceeded"
    assert app._price_book.reference("workflow:invoice_cleaning") is None

    # Review threshold: blocked until the caller acknowledges, then runs.
    held = run({"budget": {"review_threshold": 0.01}})
    assert held.status == 409
    assert held.body["error"]["code"] == "review_required"
    acknowledged = run(
        {"budget": {"review_threshold": 0.01}, "review_acknowledged": True}
    )
    assert acknowledged.status == 200, acknowledged.body
    assert acknowledged.body["status"] == "succeeded"
    assert acknowledged.body["budget"]["needs_review"] is True
    conn.close()


def test_spending_behavior_gates_outlier_runs(tmp_path):
    """No declared budget at all: the tenant's own cheap history flags an
    expensive plan for review — and acknowledging it runs."""
    app, conn, ident, registry, metering, attribution, audit = _build(
        tmp_path, executors={"cli": _CliExecutor()}
    )
    _seed_chain(app, ident, registry)
    contract = _assembled_contract(app, ident)
    _spend(attribution, gross=0.01, count=3)  # a habit of tiny runs

    held = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body={"contract": contract},
        )
    )
    assert held.status == 409
    assert "usual spending" in held.body["error"]["message"]

    acknowledged = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body={"contract": contract, "review_acknowledged": True},
        )
    )
    assert acknowledged.status == 200
    # Another tenant's history is theirs alone: fresh consumers run free.
    profile = acknowledged.body["budget"]["profile"]
    assert profile["runs"] >= 3
    conn.close()


def test_partial_wallet_reviews_but_never_refuses_the_run(tmp_path):
    app, conn, ident, registry, *_rest = _build(
        tmp_path,
        executors={"cli": _CliExecutor()},
        wallet_lookup=lambda tenant, principal: 0.001,  # a sliver linked
    )
    _seed_chain(app, ident, registry)
    contract = _assembled_contract(app, ident)
    token = ident.token("consumer", "t2")

    held = app.handle(
        _req("POST", "/v1/runs/contract", token=token, body={"contract": contract})
    )
    assert held.status == 409
    assert "may be partial" in held.body["error"]["message"]

    confirmed = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=token,
            body={"contract": contract, "review_acknowledged": True},
        )
    )
    assert confirmed.status == 200  # balance informs; behavior decides
    conn.close()
