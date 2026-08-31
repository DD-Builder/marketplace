"""Which way uncertainty moves the asking price.

The ceiling-test posture lists a piece 35% *above* the market anchor, on the sound
merchandising logic that a desirable piece nobody can quite pin down sometimes finds a
buyer who pays up. The bug was the gate: any appraisal under 0.55 confidence qualified.

That aimed the most aggressive posture at the pieces the model understood least. A
text-only appraisal is capped at 0.35 confidence by construction (appraiser.py sets the
ceiling when no photo is available), and every appraisal in the live catalogue before this
week was text-only — 25 of them, confidence 0.05 to 0.30. So in the only regime this
system had ever actually run in, "I cannot tell what this is" was the trigger for "ask a
third more than it's worth", against an anchor derived from the same non-understanding.

Uncertainty widens the range you would accept. It never raises the number you ask.
"""

from __future__ import annotations

from dealfinder.core.schemas import AppraisalResult
from dealfinder.resale import PieceCosts, Posture, suggest_resale_price

COSTS = PieceCosts(acquisition_cents=10000, materials_cents=2000, labor_hours=4.0)
RATE = 3000


def _appraisal(confidence: float, *, maker: str | None = None) -> AppraisalResult:
    """A desirable piece: restored value well over as-is, which is the other half of the
    ceiling-test gate. Only confidence varies between cases."""
    return AppraisalResult(
        identified_item="walnut credenza", maker_guess=maker,
        est_asis_value_cents=20000, est_restored_resale_value_cents=90000,
        est_restoration_cost_cents=8000, est_restoration_effort_hours=6.0,
        confidence=confidence, deal_score=40.0,
    )


def _price(confidence: float, **kw) -> int:
    return suggest_resale_price(_appraisal(confidence, **kw), COSTS, RATE).list_price_cents


def test_a_piece_the_model_cannot_identify_is_not_marked_up():
    """The regression. 0.20 confidence is the live-catalogue norm, not an edge case."""
    s = suggest_resale_price(_appraisal(0.20), COSTS, RATE)
    assert s.posture is Posture.MARKET
    assert s.list_price_cents == 99000          # market 90000 + 10% premium, no markup
    assert "soft" in s.rationale


def test_the_ceiling_test_becomes_a_range_instead_of_a_higher_recommendation():
    """Ambiguous *maker*, trustworthy *valuation* — the case the posture exists for. The
    merchandising idea survives; it just stops masquerading as the recommended price."""
    s = suggest_resale_price(_appraisal(0.50), COSTS, RATE)
    assert s.posture is Posture.CEILING_TEST
    assert s.list_price_cents == 99000, "the recommendation is still the market anchor"
    assert s.stretch_price_cents == 130500, "the opening ask is offered alongside it"
    assert "coming down" in s.rationale and "not a markup" in s.rationale


def test_a_confident_piece_is_offered_no_stretch():
    """A stretch is a hedge against not knowing. There is nothing to hedge here."""
    assert suggest_resale_price(
        _appraisal(0.80, maker="Broyhill Brasilia"), COSTS, RATE
    ).stretch_price_cents is None
    assert suggest_resale_price(_appraisal(0.20), COSTS, RATE).stretch_price_cents is None


def test_less_confidence_never_produces_a_higher_price():
    """The invariant the old gate broke, swept across the whole range. Price must never
    increase as the model's understanding decreases."""
    prices = [(c, _price(c)) for c in
              (0.05, 0.15, 0.25, 0.35, 0.44, 0.45, 0.50, 0.54, 0.55, 0.70, 0.90)]
    for (c_low, p_low), (c_high, p_high) in zip(prices, prices[1:]):
        assert p_low <= p_high, (
            f"confidence {c_low} asks {p_low} but {c_high} asks {p_high} — "
            "knowing less produced a higher price"
        )


def test_the_boundary_is_where_it_says_it_is():
    """One point of confidence must not swing the ask by a third in the wrong direction."""
    assert suggest_resale_price(_appraisal(0.44), COSTS, RATE).posture is Posture.MARKET
    assert suggest_resale_price(_appraisal(0.45), COSTS, RATE).posture is Posture.CEILING_TEST


def test_a_confidently_identified_piece_is_unaffected():
    """The known-maker path never went through the broken gate and must not move."""
    s = suggest_resale_price(_appraisal(0.80, maker="Broyhill Brasilia"), COSTS, RATE)
    assert s.posture is Posture.KNOWN_PREMIUM
    assert s.list_price_cents == 99000
    assert "Broyhill Brasilia" in s.rationale


def test_an_undesirable_piece_is_never_ceiling_tested_at_any_confidence():
    """The desirability half of the gate still stands on its own."""
    flat = AppraisalResult(
        identified_item="pine dresser", est_asis_value_cents=20000,
        est_restored_resale_value_cents=22000,   # nowhere near 1.5x
        est_restoration_cost_cents=5000, est_restoration_effort_hours=3.0,
        confidence=0.50, deal_score=10.0,
    )
    assert suggest_resale_price(flat, COSTS, RATE).posture is Posture.MARKET
