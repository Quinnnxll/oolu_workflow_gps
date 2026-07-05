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


def test_class_history_judges_its_own_class():
    """The birthday case: lavish gift spending is normal FOR GIFTS, and it
    never loosens (or gets flagged by) the tight everyday profile."""
    everyday = [0.5, 0.6, 0.4, 0.5, 0.5]
    gifts = [50.0, 60.0, 45.0]

    # A 45-unit gift: the global profile would scream, the gift-class
    # profile shrugs — and the class profile is the one with authority.
    flagged_globally = assess_budget(45.0, spend_history=everyday)
    assert flagged_globally.needs_review
    judged_in_class = assess_budget(
        45.0,
        spend_history=everyday,
        class_history=gifts,
        goal_class="workflow:gifts",
    )
    assert not judged_in_class.needs_review
    assert judged_in_class.goal_class == "workflow:gifts"
    assert judged_in_class.class_profile is not None
    assert judged_in_class.class_profile.peak == 60.0

    # And the converse: lavish gifts never loosen everyday automation.
    # Globally, the 60-unit gift peak would wave a 5-unit run through;
    # the everyday class knows better.
    mixed_global = everyday + gifts
    waved_through = assess_budget(5.0, spend_history=mixed_global)
    assert not waved_through.needs_review  # the global blind spot
    held = assess_budget(
        5.0,
        spend_history=mixed_global,
        class_history=everyday,
        goal_class="workflow:everyday",
    )
    assert held.needs_review
    assert any("workflow:everyday" in r for r in held.reasons)


def test_thin_class_history_falls_back_to_global():
    """A first lavish run in a new class gets one review (judged globally);
    afterwards the class speaks for itself."""
    everyday = [0.5, 0.6, 0.4, 0.5, 0.5]
    verdict = assess_budget(
        45.0,
        spend_history=everyday,
        class_history=[50.0],  # one gift so far: not enough to judge by
        goal_class="workflow:gifts",
    )
    assert verdict.needs_review
    (reason,) = verdict.reasons
    assert "workflow:gifts" not in reason  # the global profile judged


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
def _spend(attribution, gross, count, goal_class=None):
    for index in range(count):
        attribution.bind(
            RunBinding(
                run_id=f"hist-{goal_class}-{gross}-{index}",
                version_id="v-past",
                consumer_tenant="t2",
                consumer_principal="consumer",
                gross=gross,
                goal_class=goal_class,
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


def test_run_is_judged_within_its_own_goal_class(tmp_path):
    """Lavish spending in another class never waves an outlier through —
    and a class with its own lavish history runs free where the global
    (everyday-heavy) profile would have flagged it."""
    app, conn, ident, registry, metering, attribution, audit = _build(
        tmp_path, executors={"cli": _CliExecutor()}
    )
    _seed_chain(app, ident, registry)
    contract = _assembled_contract(app, ident)  # class workflow:invoice_cleaning
    token = ident.token("consumer", "t2")

    # Tight habits IN this class; lucrative gift spending elsewhere.
    _spend(attribution, gross=0.01, count=3, goal_class="workflow:invoice_cleaning")
    _spend(attribution, gross=100.0, count=3, goal_class="workflow:gifts")

    held = app.handle(
        _req("POST", "/v1/runs/contract", token=token, body={"contract": contract})
    )
    assert held.status == 409  # gift money does not launder invoice habits
    assert "workflow:invoice_cleaning" in held.body["error"]["message"]
    conn.close()

    # Fresh consumer whose lavish history IS this class: runs free.
    lavish_dir = tmp_path / "lavish"
    lavish_dir.mkdir()
    app, conn, ident, registry, metering, attribution, audit = _build(
        lavish_dir, executors={"cli": _CliExecutor()}
    )
    _seed_chain(app, ident, registry)
    contract = _assembled_contract(app, ident)
    _spend(attribution, gross=50.0, count=3, goal_class="workflow:invoice_cleaning")
    _spend(attribution, gross=0.01, count=5, goal_class="workflow:everyday")

    resp = app.handle(
        _req(
            "POST",
            "/v1/runs/contract",
            token=ident.token("consumer", "t2"),
            body={"contract": contract},
        )
    )
    assert resp.status == 200, resp.body  # normal for THIS class of goal
    assert resp.body["budget"]["goal_class"] == "workflow:invoice_cleaning"
    assert resp.body["budget"]["class_profile"]["peak"] == 50.0
    # The run's own binding carries the class, growing the right bucket.
    binding = attribution.get_binding(resp.body["run_id"])
    assert binding.goal_class == "workflow:invoice_cleaning"
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
