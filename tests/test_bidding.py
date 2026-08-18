"""Max-bid math and endgame dynamics.

The invariants worth pinning: the ceiling works backwards from resale economics and
never goes negative; less confidence always means a *lower* ceiling (the Marketplace
side once had that backwards — see test_resale_uncertainty.py); the endgame multiplier
learns from observed endings but is shrunk toward its prior while the sample is thin;
and the stance never says "bid" outside the final day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dealfinder.auctions.bidding import (
    DEFAULT_ENDGAME_MULTIPLIER,
    BidGuidance,
    bid_velocity_cents_per_hour,
    endgame_multiplier,
    guide,
    max_bid_cents,
    projected_final_cents,
)
from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry, BidPoint, observe_auctions
from dealfinder.core.schemas import AppraisalResult
from dealfinder.sources.ebth import AuctionItem

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _appraisal(asis=60000, restored=90000, cost=5000, hours=4.0, conf=0.8):
    """These lots are bought to resell as they arrive, so ``asis`` is the figure the
    bid math prices off — ``restored``/``cost``/``hours`` are still populated precisely
    so the tests can prove they're ignored."""
    return AppraisalResult(
        identified_item="walnut credenza", maker_guess="Lane",
        est_asis_value_cents=asis, est_restored_resale_value_cents=restored,
        est_restoration_cost_cents=cost, est_restoration_effort_hours=hours,
        confidence=conf, deal_score=60.0,
    )


def _entry(bid=10000, ends_in_h=10.0, appraisal=None, vertical="jewelry", **kw):
    """Defaults to a *shippable* vertical so the arithmetic tests carry a flat $35 rather
    than a round-trip drive; the pickup path is exercised explicitly below."""
    entry = AuctionEntry(
        id="1-lot", first_seen=NOW - timedelta(days=2), last_seen=NOW,
        ends_at=NOW + timedelta(hours=ends_in_h), vertical=vertical,
        current_bid_cents=bid, appraisal=appraisal, watch=True,
        state="ending" if ends_in_h <= 24 else "live", **kw,
    )
    entry.bid_history = [BidPoint(at=NOW - timedelta(days=2), bid_cents=bid // 2),
                         BidPoint(at=NOW, bid_cents=bid)]
    return entry


# --- the ceiling ------------------------------------------------------------------------

def test_max_bid_works_backwards_from_the_flip():
    # $600 as-is, 25% margin -> keep $450; minus $35 shipping -> $415 all-in cap; at a
    # 15% premium the hammer ceiling is $415/1.15 = $360.
    got = max_bid_cents(_appraisal(asis=60000, conf=0.8), hourly_rate_cents=3000,
                        premium_pct=0.15, shipping_cents=3500, margin_pct=0.25)
    assert got == int((60000 * 0.75 - 3500) / 1.15)


def test_restoration_cost_and_hours_are_ignored_entirely():
    """These lots are resold as they arrive. Charging the bid for a restoration nobody
    is going to do would understate the ceiling exactly as badly as pricing off the
    restored value would overstate it."""
    cheap_fix = _appraisal(asis=60000, cost=0, hours=0.0)
    huge_fix = _appraisal(asis=60000, cost=90000, hours=200.0)
    assert max_bid_cents(cheap_fix) == max_bid_cents(huge_fix)


def test_the_ceiling_prices_off_as_is_not_the_restored_dream():
    """The single easiest way to talk yourself into overpaying at auction."""
    modest_asis = _appraisal(asis=20000, restored=200000)
    assert max_bid_cents(modest_asis) < 20000


def test_a_missing_as_is_figure_falls_back_rather_than_reading_as_worthless():
    """Older stored appraisals may carry no as-is number; 0 there must not be taken to
    mean the piece is worth nothing."""
    from dealfinder.auctions.bidding import resale_value_cents

    assert resale_value_cents(_appraisal(asis=0, restored=45000)) == 45000


def test_shipping_and_premium_both_shrink_the_ceiling():
    base = max_bid_cents(_appraisal(), premium_pct=0.0, shipping_cents=0)
    with_fees = max_bid_cents(_appraisal(), premium_pct=0.2, shipping_cents=8000)
    assert with_fees < base


def test_less_confidence_always_lowers_the_ceiling():
    """The auction twin of the Marketplace invariant: uncertainty must never make the
    tool tell you to pay MORE."""
    ceilings = [max_bid_cents(_appraisal(conf=c))
                for c in (0.9, 0.7, 0.55, 0.4, 0.25, 0.1)]
    assert ceilings == sorted(ceilings, reverse=True)


def test_a_lot_that_cannot_pay_returns_zero_not_negative():
    # Worth $30 as-is; a $35 parcel alone exceeds the whole margin.
    bad = _appraisal(asis=3000)
    assert max_bid_cents(bad, shipping_cents=3500) == 0


# --- the multiplier ---------------------------------------------------------------------

def test_no_observations_means_the_prior():
    assert endgame_multiplier([]) == DEFAULT_ENDGAME_MULTIPLIER


def test_one_observation_barely_moves_the_prior():
    m = endgame_multiplier([(1000, 1000)])     # observed ratio 1.0
    assert 1.7 < m < DEFAULT_ENDGAME_MULTIPLIER


def test_a_season_of_observations_owns_the_estimate():
    pairs = [(1000, 3000)] * 60                # observed ratio 3.0, n >> weight
    assert endgame_multiplier(pairs) > 2.8


def test_the_median_shrugs_off_the_one_insane_lot():
    pairs = [(1000, 1500)] * 9 + [(1000, 40000)]
    m = endgame_multiplier(pairs)
    assert m < 2.0                              # the 40x lot does not drag the estimate


def test_multiplier_never_projects_prices_falling():
    assert endgame_multiplier([(1000, 500)] * 50) == 1.0


# --- the projection ---------------------------------------------------------------------

def test_projection_applies_fully_at_t24_and_fades_to_current_at_close():
    at_24h = projected_final_cents(_entry(bid=10000, ends_in_h=24), multiplier=2.0, now=NOW)
    at_6h = projected_final_cents(_entry(bid=10000, ends_in_h=6), multiplier=2.0, now=NOW)
    at_0h = projected_final_cents(_entry(bid=10000, ends_in_h=0), multiplier=2.0, now=NOW)
    assert at_24h == 20000
    assert at_6h == 12500        # 1 + (2-1) * 6/24
    assert at_0h == 10000


def test_velocity_needs_two_recent_points():
    entry = _entry(bid=10000)
    entry.bid_history = [BidPoint(at=NOW - timedelta(hours=2), bid_cents=4000),
                         BidPoint(at=NOW, bid_cents=10000)]
    assert bid_velocity_cents_per_hour(entry, now=NOW) == 3000.0
    entry.bid_history = entry.bid_history[-1:]
    assert bid_velocity_cents_per_hour(entry, now=NOW) is None


# --- the stance -------------------------------------------------------------------------

def _guide(**kw) -> BidGuidance:
    g = guide(_entry(**kw), multiplier=2.0, calibration_n=5, now=NOW)
    assert g is not None
    return g


def test_endgame_with_headroom_says_bid_late():
    g = _guide(bid=5000, ends_in_h=10, appraisal=_appraisal())
    assert g.stance == "bid"
    assert "late" in g.reason.lower() or "ceiling" in g.reason.lower()
    assert g.headroom_cents == g.max_bid_cents - 5000


def test_early_days_say_watch_and_warn_against_early_bids():
    g = _guide(bid=2000, ends_in_h=90, appraisal=_appraisal())
    assert g.stance == "watch"
    assert any("bid early" in n.lower() for n in g.notes)


def test_current_bid_past_ceiling_is_outpriced():
    g = _guide(bid=50000, ends_in_h=10, appraisal=_appraisal())
    assert g.stance == "outpriced"


def test_projection_past_ceiling_is_outpriced_even_with_headroom_today():
    """The tracker's whole edge: seeing that a $100 lot with a day left is already a
    $200 lot, before the bids arrive."""
    appraisal = _appraisal(asis=30000)
    ceiling = max_bid_cents(appraisal, shipping_cents=3500)
    g = guide(_entry(bid=int(ceiling * 0.7), ends_in_h=23, appraisal=appraisal),
              multiplier=2.0, calibration_n=5, now=NOW)
    assert g.stance == "outpriced"
    assert "projection" in g.reason.lower() or "endgame" in g.reason.lower()


# --- logistics: shipping vs. the drive to Cincinnati -------------------------------------

def test_a_shippable_lot_is_charged_a_flat_parcel_rate():
    g = _guide(bid=5000, ends_in_h=10, appraisal=_appraisal(), vertical="jewelry")
    assert g.logistics_label == "Shipping"
    assert g.logistics_cents == 3500


def test_a_bulky_lot_is_charged_the_round_trip_instead():
    """Furniture and rugs don't go in a box — the honest cost is the drive, both the
    mileage and the half-day it takes."""
    g = _guide(bid=5000, ends_in_h=10, appraisal=_appraisal(asis=200000),
               vertical="furniture")
    assert g.logistics_label == "Pickup drive"
    # 166 mi round trip at $0.70 + 2.9h of your time at $30/hr.
    assert g.logistics_cents == round(166 * 70) + round(2.9 * 3000)
    assert "round trip" in g.logistics_detail


def test_bulkiness_lowers_the_ceiling_for_an_otherwise_identical_lot():
    """The whole point of splitting them: a $200 drive vs. a $35 parcel is enough to
    flip a marginal lot, so it must actually move the number."""
    appraisal = _appraisal(asis=200000)
    shipped = _guide(bid=1000, ends_in_h=10, appraisal=appraisal, vertical="jewelry")
    driven = _guide(bid=1000, ends_in_h=10, appraisal=appraisal, vertical="furniture")
    assert driven.max_bid_cents < shipped.max_bid_cents


def test_guidance_reports_the_value_it_priced_off_and_the_margin_at_todays_bid():
    g = _guide(bid=5000, ends_in_h=10, appraisal=_appraisal(asis=60000),
               vertical="jewelry")
    assert g.value_cents == 60000
    # Worth $600, bid $50 + 15% premium + $35 shipping -> about $507 of margin.
    assert g.margin_at_current_cents == 60000 - round(5000 * 1.15) - 3500


def test_worthless_economics_say_pass():
    # Worth $30 as-is — a $35 parcel alone exceeds the entire margin, so there is no
    # bid at any price that pays.
    g = _guide(bid=1000, ends_in_h=10, appraisal=_appraisal(asis=3000),
               vertical="jewelry")
    assert g.stance == "no-value"
    assert g.max_bid_cents == 0


def test_unappraised_lots_get_no_guidance():
    assert guide(_entry(appraisal=None), multiplier=2.0, now=NOW) is None


def test_thin_calibration_is_said_out_loud():
    g = guide(_entry(bid=5000, ends_in_h=10, appraisal=_appraisal()),
              multiplier=2.0, calibration_n=1, now=NOW)
    assert any("1 observed" in n for n in g.notes)


# --- integration with the catalogue -----------------------------------------------------

def test_calibration_flows_from_observed_endings_into_guidance():
    """End-to-end: watch a lot, see it close, and the next lot's projection uses what
    the first one taught."""
    cat = AuctionCatalog()
    ends = NOW + timedelta(hours=30)

    def item(bid):
        return AuctionItem(item_id="cal-1", title="Teak Desk",
                           current_bid_cents=bid, ends_at=ends)

    observe_auctions(cat, [item(1000)], now=NOW)
    observe_auctions(cat, [item(1000)], now=ends - timedelta(hours=25))
    observe_auctions(cat, [item(3000)], now=ends + timedelta(minutes=30))

    from dealfinder.auctions.catalog import calibration_pairs

    pairs = calibration_pairs(cat)
    assert pairs == [(1000, 3000)]
    m = endgame_multiplier(pairs)
    assert m > DEFAULT_ENDGAME_MULTIPLIER * 0.9   # 3.0 observation pulls the 2.0 prior up
