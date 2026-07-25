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


def test_hot_but_ambiguous_prices_above_market():
    s = suggest_resale_price(
        _appraisal(restored=30000, asis=10000, conf=0.4, maker=None),
        PieceCosts(acquisition_cents=4000, materials_cents=1000, labor_hours=2.0),
        RATE,
    )
    assert s.posture is Posture.CEILING_TEST
    assert s.list_price_cents > 30000  # priced above market to test the ceiling


def test_ambiguous_but_not_desirable_stays_at_market():
    # low confidence but restored value barely above as-is -> not a ceiling-test candidate
    s = suggest_resale_price(
        _appraisal(restored=11000, asis=10000, conf=0.4, maker=None),
        PieceCosts(acquisition_cents=4000, labor_hours=1.0),
        RATE,
    )
    assert s.posture is Posture.MARKET


def test_list_price_never_below_cost_floor():
    # Market value is low, but costs+labor are high -> floor protects you.
    costs = PieceCosts(acquisition_cents=6000, materials_cents=3000, labor_hours=5.0)
    s = suggest_resale_price(_appraisal(restored=5000, conf=0.9, maker="Lane"), costs, RATE)
    assert s.list_price_cents == s.floor_price_cents
    assert s.floor_price_cents >= loaded_cost_cents(costs, RATE)


def test_underwater_piece_is_flagged_not_given_a_fantasy_target():
    # Break-even needs far more than the piece is worth restored -> don't pretend a
    # sell target exists. (Regression: a real run suggested a $632 target on a piece
    # the appraiser valued at $220.)
    costs = PieceCosts(acquisition_cents=7500, materials_cents=4000, labor_hours=6.0)
    s = suggest_resale_price(_appraisal(restored=22000, conf=0.3, maker=None), costs, RATE)
    assert not s.viable
    assert "underwater" in s.warning.lower()


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
