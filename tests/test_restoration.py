"""Bounds on the model's restoration estimate.

These two fields are invented from a photograph and feed the deal score twice over — cost
comes off the margin, hours are multiplied by the hourly rate and come off it again. A
hallucinated "40 hours" silently rejects a good piece; a hallucinated "$5, one hour" waves
through a bad one.
"""

from __future__ import annotations

from dealfinder.restoration import clamp_restoration


def test_a_plausible_estimate_is_left_alone():
    """The common case must be a no-op, or the board fills with noise about corrections."""
    r = clamp_restoration(6000, 6.0, hourly_rate_cents=3000, restored_value_cents=90000)
    assert r.cost_cents == 6000 and r.effort_hours == 6.0
    assert not r.adjusted


def test_absurd_effort_is_capped_at_a_working_week():
    r = clamp_restoration(5000, 400.0, hourly_rate_cents=3000)
    assert r.effort_hours == 40.0
    assert "capped" in r.adjustments[0]


def test_zero_effort_is_raised_off_the_floor():
    """Zero hours makes labour free, which makes every piece look profitable."""
    r = clamp_restoration(5000, 0.0, hourly_rate_cents=3000)
    assert r.effort_hours == 0.5
    assert r.adjusted


def test_materials_cannot_dwarf_the_labour_they_accompany():
    """Published surveys put labour at ~85% of a refinish. Materials far above the labour
    value means the model conflated 'what it's worth' with 'what fixing it costs'."""
    # 2 hours at $30 = $60 of labour, but $900 of claimed materials.
    r = clamp_restoration(90000, 2.0, hourly_rate_cents=3000)
    assert r.cost_cents <= 6000
    assert any("85%" in n for n in r.adjustments)


def test_restoration_never_costs_more_than_the_piece_will_fetch():
    r = clamp_restoration(200000, 30.0, hourly_rate_cents=3000, restored_value_cents=40000)
    assert r.cost_cents <= 40000
    assert any("no one spends more" in n for n in r.adjustments)


def test_the_clamp_reaches_pieces_already_in_the_catalogue():
    """Clamping lives in evaluate_piece, not at appraisal time, so published cost reality
    applies retroactively to every stored appraisal rather than only to new ones."""
    from dealfinder.core.schemas import AppraisalResult, RawListing
    from dealfinder.engine import evaluate_piece

    listing = RawListing(fb_listing_id="x", title="walnut credenza", asking_price_cents=20000)
    absurd = AppraisalResult(
        identified_item="credenza", est_asis_value_cents=20000,
        est_restored_resale_value_cents=90000, est_restoration_cost_cents=5000,
        est_restoration_effort_hours=500.0,          # 500 hours of work
        confidence=0.8, deal_score=50.0,
    )
    piece = evaluate_piece(listing, absurd, hourly_rate_cents=3000)
    assert piece.appraisal.est_restoration_effort_hours == 40.0
    assert piece.restoration_notes, "the correction must be visible, not silent"
