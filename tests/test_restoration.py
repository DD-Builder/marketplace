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
    r = clamp_restoration(6000, 6.0, restored_value_cents=90000)
    assert r.cost_cents == 6000 and r.effort_hours == 6.0
    assert not r.adjusted


def test_absurd_effort_is_capped_at_a_working_week():
    r = clamp_restoration(5000, 400.0)
    assert r.effort_hours == 40.0
    assert "capped" in r.adjustments[0]


def test_zero_effort_is_raised_off_the_floor():
    """Zero hours makes labour free, which makes every piece look profitable."""
    r = clamp_restoration(5000, 0.0)
    assert r.effort_hours == 0.5
    assert r.adjusted


def test_restoration_never_costs_more_than_the_piece_will_fetch():
    r = clamp_restoration(200000, 30.0, restored_value_cents=40000)
    assert r.cost_cents <= 40000
    assert any("no one spends more" in n for n in r.adjustments)


def test_a_parts_heavy_job_is_not_cut_down_to_its_labour():
    """Regression. An earlier rule capped materials at ``hours x hourly_rate``, reasoning
    from the surveys' "labour is ~85% of a refinish". It fired hardest on exactly the jobs
    where materials legitimately dominate: a $400 marble top fitted in 1.5 hours was cut to
    $45, and since cost is subtracted from margin the "sanity bound" made the piece look
    2x better than it was. A clamp on this field may only ever be pessimistic."""
    r = clamp_restoration(40000, 1.5, restored_value_cents=90000)
    assert r.cost_cents == 40000, "a real parts cost must survive a short job"
    assert not r.adjusted


def test_no_bound_depends_on_your_hourly_rate():
    """The stored appraisal must not become a function of a personal config value: raising
    HOURLY_RATE_CENTS between runs could otherwise never restore a materials figure that a
    lower rate had already cut."""
    assert clamp_restoration(40000, 2.0).cost_cents == clamp_restoration(40000, 2.0).cost_cents
    import inspect
    assert "hourly_rate" not in str(inspect.signature(clamp_restoration))


def test_every_note_states_the_value_actually_assigned():
    """A note that quotes a target the floor then overrode is a lie about the number on the
    card. Here the restored value ($4) is below the materials floor ($5)."""
    r = clamp_restoration(1000, 1.0, restored_value_cents=400)
    assert r.cost_cents == 500
    assert "to $5" in r.adjustments[0], r.adjustments


def test_clamping_is_idempotent_and_reports_nothing_the_second_time():
    """The board re-clamps stored appraisals on every render. If a clamped value could
    still trip a rule, every card would show a bogus correction forever."""
    for cost, hours in [(1000, 1.0), (300, 0.5), (500000, 2.0), (40000, 1.5), (5000, 400.0)]:
        once = clamp_restoration(cost, hours, restored_value_cents=90000)
        twice = clamp_restoration(once.cost_cents, once.effort_hours,
                                  restored_value_cents=90000)
        assert not twice.adjusted, f"{(cost, hours)} -> {twice.adjustments}"
        assert twice.cost_cents == once.cost_cents
        assert twice.effort_hours == once.effort_hours


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
    # ...and the model's own answer survives alongside it, because that is what gets
    # stored. Persisting the clamped copy would destroy the original on first write.
    assert piece.appraisal_raw.est_restoration_effort_hours == 500.0
