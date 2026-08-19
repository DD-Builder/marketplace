"""Comp-weighted valuation: a distribution over realised sales, not an asserted number.

The case behind every test here is a Nino Pippa oil the engine valued at $1,200 and set a
$690 maximum bid on, while the artist's realised results run $111-$401 and 26 bidders had
it at $250. The engine had reasoned from "working regional landscape artists of this
calibre" — a plausible figure attached to no transaction.
"""

from __future__ import annotations

import pytest

from dealfinder.valuation.artcomps import SoldComp, estimate

THIS_YEAR = 2026


def _pippa_comps() -> list[SoldComp]:
    """The artist's actual realised results, plus his artist-direct asking price."""
    return [
        SoldComp(price_cents=40100, title="Tuscany Wild Flowers", medium="oil on board",
                 width_in=12, height_in=16, year_sold=2025, venue="EBTH"),
        SoldComp(price_cents=11100, title="Venice Side Canal", medium="oil on board",
                 width_in=9, height_in=12, year_sold=2025, venue="EBTH"),
        SoldComp(price_cents=38000, medium="oil on board",
                 width_in=12, height_in=16, year_sold=2024),
        SoldComp(price_cents=15000, medium="oil on board",
                 width_in=9, height_in=12, year_sold=2024),
        SoldComp(price_cents=30000, medium="oil on board",
                 width_in=12, height_in=16, year_sold=2023),
        SoldComp(price_cents=240000, medium="oil on board", width_in=16, height_in=20,
                 year_sold=2026, venue="artist's own eBay store", is_sold=False),
    ]


def _target(medium: str = "oil on board") -> SoldComp:
    return SoldComp(price_cents=0, medium=medium, width_in=12, height_in=16)


def test_the_estimate_lands_in_the_artists_actual_market():
    """Two independent research passes put this piece at $150-350 and $150-500. The
    engine's own figure was $1,200."""
    band = estimate(_pippa_comps(), target=_target(), this_year=THIS_YEAR)

    assert band is not None
    assert 15000 <= band.mid_cents <= 50000, f"${band.mid_cents / 100:,.0f}"
    assert band.mid_cents < 120000 / 2, "nowhere near the $1,200 point estimate"


def test_an_asking_price_can_never_lift_the_estimate_above_a_realised_one():
    """The artist sells direct at $2,400 and his work hammers at $111-$401. An ask is not
    evidence that anything changed hands, so it must not set the ceiling — tuning the ask
    weight down until it stopped mattering would be fitting to one artist."""
    band = estimate(_pippa_comps(), target=_target(), this_year=THIS_YEAR)
    realised_max = max(c.price_cents for c in _pippa_comps() if c.is_sold)

    assert band is not None
    assert band.mid_cents <= realised_max
    assert band.high_cents <= realised_max


def test_a_reproduction_is_not_valued_off_originals():
    """A textured giclée is built to be mistaken for the oil it copies and can carry the
    same title. Conflating them is the error that inflates a valuation."""
    band = estimate(_pippa_comps(), target=_target("giclée print on canvas"),
                    this_year=THIS_YEAR)

    assert band is None, "oil comps must not price a print"


def test_a_single_stray_comp_is_not_a_valuation():
    """Returning nothing routes the lot to the market anchor, which at least knows what
    the room is paying. Dressing one record up as an estimate does not."""
    assert estimate([SoldComp(price_cents=50000, medium="oil on canvas", year_sold=2019)],
                    target=_target("oil on canvas"), this_year=THIS_YEAR) is None


def test_asks_alone_never_produce_an_estimate():
    """A market with no observed transactions has no observed price."""
    asks = [SoldComp(price_cents=240000, medium="oil on board", width_in=12, height_in=16,
                     year_sold=2026, is_sold=False) for _ in range(8)]

    assert estimate(asks, target=_target(), this_year=THIS_YEAR) is None


def test_recent_results_outweigh_stale_ones():
    """A 2015 result is evidence about 2015."""
    comps = [
        SoldComp(price_cents=200000, medium="oil on board", width_in=12, height_in=16,
                 year_sold=2008),
        SoldComp(price_cents=200000, medium="oil on board", width_in=12, height_in=16,
                 year_sold=2009),
        SoldComp(price_cents=20000, medium="oil on board", width_in=12, height_in=16,
                 year_sold=2026),
        SoldComp(price_cents=22000, medium="oil on board", width_in=12, height_in=16,
                 year_sold=2025),
    ]
    band = estimate(comps, target=_target(), this_year=THIS_YEAR)

    assert band is not None
    assert band.mid_cents < 100000, "the two decade-old results must not carry the median"


def test_size_proximity_is_symmetric():
    """A comp at twice the area and one at half are equally distant, and neither should
    be treated as the same object."""
    from dealfinder.valuation.artcomps import comp_weight

    target = _target()          # 12x16 = 192 sq in
    double = SoldComp(price_cents=1, medium="oil on board", width_in=16, height_in=24)
    half = SoldComp(price_cents=1, medium="oil on board", width_in=8, height_in=12)

    w_double = comp_weight(double, target=target, this_year=THIS_YEAR)
    w_half = comp_weight(half, target=target, this_year=THIS_YEAR)
    assert w_double == pytest.approx(w_half, rel=0.15)


def test_the_band_reports_its_own_evidence():
    """A valuation you cannot audit is how $1,200 survived. The basis line names how many
    realised sales are behind the number and their raw range."""
    band = estimate(_pippa_comps(), target=_target(), this_year=THIS_YEAR)

    assert band is not None
    assert band.n_sold == 5
    assert "realised sale" in band.basis
    assert band.spread_ratio > 1.0, "an artist with a 4x range does not have 'a value'"
