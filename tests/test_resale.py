"""Resale pricing + economics ledger tests."""

from __future__ import annotations

from dealfinder.core.schemas import AppraisalResult
from dealfinder.resale import (
    PieceCosts,
    Posture,
    cash_outlay_cents,
    loaded_cost_cents,
    realized,
    suggest_resale_price,
)

RATE = 3000  # $30/hr in cents


def _appraisal(*, restored, asis=10000, conf=0.8, maker="Lane"):
    return AppraisalResult(
        identified_item="end table",
        maker_guess=maker,
        est_asis_value_cents=asis,
        est_restored_resale_value_cents=restored,
        est_restoration_cost_cents=2000,
        est_restoration_effort_hours=3.0,
        confidence=conf,
        deal_score=50.0,
    )


def test_cost_basis_includes_labor():
    costs = PieceCosts(acquisition_cents=5000, materials_cents=2000, labor_hours=4.0)
    assert cash_outlay_cents(costs) == 7000
    assert loaded_cost_cents(costs, RATE) == 7000 + 12000  # + 4h * $30


def test_known_item_anchors_to_market_plus_premium():
    s = suggest_resale_price(
        _appraisal(restored=40000, conf=0.85, maker="Lane"),
        PieceCosts(acquisition_cents=5000, materials_cents=2000, labor_hours=3.0),
        RATE,
        premium_pct=0.10,
    )
    assert s.posture is Posture.KNOWN_PREMIUM
    assert s.list_price_cents == 44000  # 40000 + 10%


def test_hot_but_ambiguous_offers_a_higher_opening_ask():
    """Updated: the markup moved from the recommendation to an explicit opening ask.

    Confidence also moved from 0.4 to 0.5, because 0.4 no longer qualifies at all. A
    ceiling test extrapolates upward from the market anchor, so it needs an anchor worth
    extrapolating from; below 0.45 the model has not identified the piece well enough for
    its own estimate to bear a 35% markup. See tests/test_resale_uncertainty.py.
    """
    s = suggest_resale_price(
        _appraisal(restored=30000, asis=10000, conf=0.5, maker=None),
        PieceCosts(acquisition_cents=4000, materials_cents=1000, labor_hours=2.0),
        RATE,
    )
    assert s.posture is Posture.CEILING_TEST
    assert s.list_price_cents == 33000            # market + premium, unmarked-up
    assert s.stretch_price_cents > 33000          # the ceiling is offered, not imposed


def test_ambiguous_but_not_desirable_stays_at_market():
    # low confidence but restored value barely above as-is -> not a ceiling-test candidate
    s = suggest_resale_price(
        _appraisal(restored=11000, asis=10000, conf=0.4, maker=None),
        PieceCosts(acquisition_cents=4000, labor_hours=1.0),
        RATE,
    )
    assert s.posture is Posture.MARKET


def test_floor_is_advisory_not_the_asking_price():
    # Costs+labour far exceed what the piece is worth. The floor is still reported (it's
    # the walk-away), but it must NOT become the list price — pricing to your costs rather
    # than the market just means it never sells.
    costs = PieceCosts(acquisition_cents=6000, materials_cents=3000, labor_hours=5.0)
    s = suggest_resale_price(_appraisal(restored=5000, conf=0.9, maker="Lane"), costs, RATE)
    assert s.floor_price_cents >= loaded_cost_cents(costs, RATE)  # still surfaced
    assert s.list_price_cents < s.floor_price_cents               # but priced to market
    assert s.status == "underwater"


def test_cash_negative_piece_is_a_real_skip():
    # Sells for less than you'd have in it out of pocket -> don't buy at any hourly rate.
    costs = PieceCosts(acquisition_cents=40000, materials_cents=4000, labor_hours=6.0)
    s = suggest_resale_price(_appraisal(restored=32000, conf=0.3, maker=None), costs, RATE)
    assert s.status == "underwater" and not s.viable
    assert "loses money" in s.warning.lower()


def test_cash_positive_but_slow_is_thin_not_a_skip():
    # A $20 piece worth $200 restored makes real cash even if the hours don't pay $30/hr.
    # Flagging this as "skip" would talk a hobbyist out of a genuinely good buy.
    # (Regression: a real run labelled a +$250-margin table "underwater".)
    costs = PieceCosts(acquisition_cents=2000, materials_cents=1500, labor_hours=20.0)
    s = suggest_resale_price(_appraisal(restored=20000, conf=0.4, maker=None), costs, RATE)
    assert s.status == "thin"
    assert s.viable  # still worth buying
    assert s.list_price_cents > 0  # and we still tell you what to ask
    assert "cash" in s.warning.lower()


def test_healthy_piece_stays_viable():
    costs = PieceCosts(acquisition_cents=5000, materials_cents=2000, labor_hours=3.0)
    s = suggest_resale_price(_appraisal(restored=40000, conf=0.85, maker="Lane"), costs, RATE)
    assert s.viable and not s.warning


def test_realized_profit_and_hourly_wage():
    costs = PieceCosts(acquisition_cents=5000, materials_cents=2000, labor_hours=4.0)
    out = realized(sale_price_cents=30000, costs=costs, hourly_rate_cents=RATE)
    assert out.cash_profit_cents == 30000 - 7000            # 23000
    assert out.net_profit_cents == 30000 - (7000 + 12000)   # 11000
    assert out.effective_hourly_cents == round(23000 / 4)   # $57.50/hr on the labor
    assert out.return_on_cash_pct == round(100 * 23000 / 7000, 1)


def test_realized_handles_zero_labor():
    out = realized(20000, PieceCosts(acquisition_cents=5000), RATE)
    assert out.effective_hourly_cents is None  # no hours logged -> no wage


def test_thin_piece_lists_at_market_not_an_unreachable_floor():
    # A $450 piece must not be listed at $978 because the labour was expensive —
    # it would simply never sell. (Regression from a real run.)
    costs = PieceCosts(acquisition_cents=5000, materials_cents=15000, labor_hours=20.0)
    s = suggest_resale_price(_appraisal(restored=45000, conf=0.4, maker=None), costs, RATE)
    assert s.status == "thin"
    # Priced off the market (posture markup allowed), never off the unreachable floor.
    assert s.list_price_cents < s.floor_price_cents
    assert s.floor_price_cents > s.market_anchor_cents  # floor stays visible as advice


# --- two-tier pricing -----------------------------------------------------------------

def test_the_market_number_never_moves_when_you_log_your_time():
    """The whole point of splitting the two: a buyer doesn't care that you spent a
    weekend on it, so the headline price must not budge."""
    from dealfinder.resale import price_piece

    appraisal = _appraisal(restored=45000, conf=0.9, maker="Lane")
    bare = price_piece(appraisal, asking_price_cents=12000, hourly_rate_cents=RATE)
    logged = price_piece(
        appraisal, asking_price_cents=12000, hourly_rate_cents=RATE,
        logged_costs=PieceCosts(acquisition_cents=12000, materials_cents=4000, labor_hours=6.0),
    )
    assert bare.headline_cents == logged.headline_cents
    assert bare.market == logged.market
    # ...but everything personal does move.
    assert logged.yours.cash_outlay_cents == 16000
    assert logged.yours.loaded_cost_cents == 16000 + 6 * RATE
    assert logged.yours.floor_price_cents != bare.yours.floor_price_cents
    assert logged.yours.logged and not bare.yours.logged


def test_the_market_tier_is_computed_without_any_of_your_costs():
    from dealfinder.resale import price_piece

    plan = price_piece(_appraisal(restored=40000), asking_price_cents=39000,
                       hourly_rate_cents=RATE)
    # 40000 market + 10% premium, whatever you paid.
    assert plan.market.list_price_cents == 44000
    assert plan.market.floor_price_cents == 0        # no costs went into it
    assert plan.market.status == "ok"


def test_your_tier_projects_profit_and_an_effective_hourly_wage():
    from dealfinder.resale import price_piece

    plan = price_piece(
        _appraisal(restored=40000, conf=0.9, maker="Lane"),
        hourly_rate_cents=RATE,
        logged_costs=PieceCosts(acquisition_cents=8000, materials_cents=4000, labor_hours=6.0),
    )
    y = plan.yours
    assert plan.headline_cents == 44000
    assert y.projected.cash_profit_cents == 44000 - 12000
    assert y.projected.net_profit_cents == 44000 - (12000 + 6 * RATE)
    assert y.projected.effective_hourly_cents == round(32000 / 6)
    assert y.status == "ok"


def test_an_unlogged_piece_is_estimated_as_bought_at_ask():
    from dealfinder.resale import price_piece

    appraisal = _appraisal(restored=40000)           # restoration: $20 materials, 3h
    plan = price_piece(appraisal, asking_price_cents=9000, hourly_rate_cents=RATE)
    assert not plan.yours.logged
    assert plan.yours.costs == PieceCosts(
        acquisition_cents=9000, materials_cents=2000, labor_hours=3.0
    )


def test_the_underwater_verdict_still_reaches_the_personal_tier():
    from dealfinder.resale import price_piece

    plan = price_piece(
        _appraisal(restored=32000, conf=0.3, maker=None), hourly_rate_cents=RATE,
        logged_costs=PieceCosts(acquisition_cents=40000, materials_cents=4000, labor_hours=6.0),
    )
    assert plan.yours.status == "underwater" and plan.yours.warning
    assert plan.market.list_price_cents > 0          # the piece is still worth what it's worth


def test_the_range_spans_the_plain_estimate_to_the_highest_ask_worth_trying():
    """Updated: the top of the range is the *stretch*, not the recommendation.

    This test used to assert `list_price_cents > 44000` for an ambiguous piece — that is,
    it pinned the bug. The ceiling markup was folded into the recommended ask, so the
    range reported its own inflation back and the card told you to ask a third more for a
    piece the model could not identify. The markup now lives in stretch_price_cents, so
    the range still widens with ambiguity while the recommendation stays at market.
    """
    from dealfinder.resale import price_piece

    low, high = price_piece(_appraisal(restored=40000), hourly_rate_cents=RATE).range_cents
    assert (low, high) == (40000, 44000)

    ceiling = price_piece(
        _appraisal(restored=40000, asis=10000, conf=0.5, maker=None), hourly_rate_cents=RATE
    )
    assert ceiling.market.posture is Posture.CEILING_TEST
    assert ceiling.market.list_price_cents == 44000, "recommendation stays anchored"
    assert ceiling.market.stretch_price_cents > 44000, "the ambiguity widens the range"
    assert ceiling.range_cents == (40000, ceiling.market.stretch_price_cents)
