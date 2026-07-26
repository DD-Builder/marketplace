"""Deal-score formula tests."""

from __future__ import annotations

from dealfinder.core.schemas import AppraisalResult
from dealfinder.valuation.scoring import compute_deal_score


def _appraisal(**over) -> AppraisalResult:
    base = dict(
        identified_item="teak sideboard",
        est_asis_value_cents=15000,
        est_restored_resale_value_cents=60000,
        est_restoration_cost_cents=5000,
        est_restoration_effort_hours=4.0,
        confidence=1.0,
        deal_score=70.0,
    )
    base.update(over)
    return AppraisalResult(**base)


def test_positive_margin_scores_above_zero():
    # net = 60000 - 12000 asking - 5000 cost - (4h * 3000) = 31000c => $310
    score = compute_deal_score(_appraisal(), asking_price_cents=12000, hourly_rate_cents=3000)
    assert 0 < score < 100
    # $310 margin -> 100 * 310/(310+500) ~= 38.3 (pre-confidence; half-point at $500)
    assert 34 < score < 42


def test_negative_margin_scores_zero():
    score = compute_deal_score(
        _appraisal(est_restored_resale_value_cents=15000),
        asking_price_cents=12000,
        hourly_rate_cents=3000,
    )
    assert score == 0.0


def test_confidence_scales_score():
    high = compute_deal_score(_appraisal(confidence=1.0), 12000, 3000)
    low = compute_deal_score(_appraisal(confidence=0.5), 12000, 3000)
    assert low == round(high * 0.5, 2)


def test_missing_asking_price_is_zero():
    assert compute_deal_score(_appraisal(), asking_price_cents=None, hourly_rate_cents=3000) == 0.0
