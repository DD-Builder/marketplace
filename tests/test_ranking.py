"""Viewing-priority / liquidity / heat tests."""

from __future__ import annotations

from dealfinder.authenticity import AuthenticityAssessment, assess_authenticity
from dealfinder.core.schemas import RawListing
from dealfinder.ranking import (
    badges,
    heat_score,
    is_killer_deal,
    liquidity_score,
    viewing_priority,
)

CLEAR = AuthenticityAssessment(verdict="clear", is_red_flag=False, value_basis="genuine_ok")
FAKE = assess_authenticity(RawListing(fb_listing_id="x", title="Eames style chair"))


def test_maker_piece_is_more_liquid_than_noname():
    named = liquidity_score(maker_guess="Lane", confidence=0.8, identified_item="end table", authenticity=CLEAR)
    noname = liquidity_score(maker_guess=None, confidence=0.5, identified_item="end table", authenticity=CLEAR)
    assert named > noname


def test_lookalike_tanks_liquidity():
    genuine = liquidity_score(maker_guess="Eames", confidence=0.8, identified_item="lounge chair", authenticity=CLEAR)
    fake = liquidity_score(maker_guess="Eames", confidence=0.8, identified_item="lounge chair", authenticity=FAKE)
    assert fake < genuine


def test_slow_category_penalised():
    dresser = liquidity_score(maker_guess=None, confidence=0.6, identified_item="dresser", authenticity=CLEAR)
    armoire = liquidity_score(maker_guess=None, confidence=0.6, identified_item="armoire", authenticity=CLEAR)
    assert armoire < dresser


def test_price_drop_and_hot_keywords_raise_heat():
    cold = heat_score(text="plain dresser", prescreen_score=0, price_dropped=False)
    hot = heat_score(text="walnut mid-century dresser", prescreen_score=3, price_dropped=True)
    assert hot > cold


def test_priority_breaks_ties_by_liquidity_and_heat():
    a = viewing_priority(deal_score=60, liquidity=90, heat=80, authenticity=CLEAR)
    b = viewing_priority(deal_score=60, liquidity=30, heat=20, authenticity=CLEAR)
    assert a > b


def test_authenticity_flag_penalises_priority():
    clean = viewing_priority(deal_score=80, liquidity=70, heat=60, authenticity=CLEAR)
    faked = viewing_priority(deal_score=80, liquidity=70, heat=60, authenticity=FAKE)
    assert faked < clean


def test_out_of_radius_penalises_priority():
    near = viewing_priority(deal_score=70, liquidity=60, heat=50, authenticity=CLEAR)
    far = viewing_priority(deal_score=70, liquidity=60, heat=50, authenticity=CLEAR, out_of_radius=True)
    assert far < near


def test_roi_lifts_cheap_high_multiple():
    from dealfinder.ranking import roi_to_score
    assert roi_to_score(25000, 2500) == 100.0   # 10x -> maxed
    assert roi_to_score(30000, 20000) < 40       # 1.5x -> modest
    low = viewing_priority(deal_score=20, liquidity=50, heat=50, authenticity=CLEAR, roi_score=0)
    high = viewing_priority(deal_score=20, liquidity=50, heat=50, authenticity=CLEAR, roi_score=100)
    assert high > low


def test_killer_requires_genuine():
    assert is_killer_deal(deal_score=80, confidence=0.8, authenticity=CLEAR)
    assert not is_killer_deal(deal_score=80, confidence=0.8, authenticity=FAKE)
    assert not is_killer_deal(deal_score=50, confidence=0.8, authenticity=CLEAR)


def test_killer_on_high_return_multiple_for_cheap_piece():
    # $20 armoire that nets $250 -> a killer by multiple even at a modest deal_score.
    assert is_killer_deal(
        deal_score=30, confidence=0.6, authenticity=CLEAR,
        net_margin_cents=25000, asking_price_cents=2000,
    )
    # Same multiple but a look-alike -> not a killer.
    assert not is_killer_deal(
        deal_score=30, confidence=0.6, authenticity=FAKE,
        net_margin_cents=25000, asking_price_cents=2000,
    )


def test_badges_surface_star_and_warning():
    b = badges(killer=True, heat=70, liquidity=80, price_dropped=True, authenticity=FAKE)
    labels = {x.label for x in b}
    assert "Killer deal" in labels and "Look-alike" in labels and "Price drop" in labels
