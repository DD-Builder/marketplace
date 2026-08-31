"""Pluggable-vertical tests: pre-screen adapts to the category it's handed."""

from __future__ import annotations

from dealfinder.core.schemas import RawListing, RawPhoto
from dealfinder.prescreen import prescreen
from dealfinder.verticals import (
    ART,
    COLLECTIBLES,
    ELECTRONICS,
    FURNITURE,
    JEWELRY,
    get_vertical,
)


def _l(title="", desc="", price=5000):
    return RawListing(
        fb_listing_id="x", title=title, description=desc,
        asking_price_cents=price, photos=[RawPhoto(remote_url="u")],
    )


def test_get_vertical_defaults_to_furniture():
    assert get_vertical(None) is FURNITURE
    assert get_vertical("nonsense") is FURNITURE
    assert get_vertical("art") is ART


def test_furniture_signal_still_works():
    assert prescreen(_l(title="Vintage teak Danish sideboard"), FURNITURE).score >= 1


def test_electronics_rejects_dead_units_but_keeps_working_gear():
    assert not prescreen(_l(title="Marantz receiver", desc="for parts not working"), ELECTRONICS).keep
    good = prescreen(_l(title="Marantz 2270 receiver", desc="tested and serviced"), ELECTRONICS)
    assert good.keep and good.score >= 1


def test_art_rejects_reproductions():
    assert not prescreen(_l(title="Framed canvas print reproduction"), ART).keep
    assert prescreen(_l(title="Original oil on canvas, signed"), ART).score >= 1


def test_vertical_price_window_is_respected():
    # Electronics floor is $10; a $2 listing is below the sane window for that vertical.
    r = prescreen(_l(title="pioneer amplifier", price=200), ELECTRONICS)
    assert any("price implausibly low" in reason for reason in r.reasons)
    assert r.keep is False   # noted-but-kept let $1 junk through to paid appraisal


def test_jewelry_rejects_costume_but_keeps_marked_precious_metal():
    assert not prescreen(
        _l(title="Costume jewelry rhinestone brooch, gold tone"), JEWELRY
    ).keep
    good = prescreen(_l(title="14k gold diamond ring", desc="sterling silver band"), JEWELRY)
    assert good.keep and good.score >= 1


def test_jewelry_recognizes_named_makers():
    r = prescreen(_l(title="Vintage Tiffany sterling silver bracelet"), JEWELRY)
    assert r.keep and any("maker:tiffany" in reason for reason in r.reasons)


def test_collectibles_rejects_silverplate_but_keeps_sterling():
    assert not prescreen(
        _l(title="Silverplate flatware set, reproduction"), COLLECTIBLES
    ).keep
    good = prescreen(_l(title="Sterling silver tea set", desc="hallmarked, antique"),
                     COLLECTIBLES)
    assert good.keep and good.score >= 1


def test_collectibles_recognizes_watch_and_silver_makers():
    r = prescreen(_l(title="Vintage Rolex automatic movement watch"), COLLECTIBLES)
    assert r.keep and any("maker:rolex" in reason for reason in r.reasons)


def test_every_vertical_is_reachable_by_key():
    for key, vertical in (("jewelry", JEWELRY), ("collectibles", COLLECTIBLES)):
        assert get_vertical(key) is vertical


def test_the_art_vertical_recognises_how_auction_houses_title_paintings():
    """Measured against 154 live EBTH art lots, the original keyword list scored 139 of
    them at zero — it named techniques ("serigraph", "oil on canvas") but not the plain
    nouns real lot titles use, so not one art lot could clear the watchlist gate and the
    board carried no art at all despite art being explicitly asked for."""
    from dealfinder.core.schemas import RawListing
    from dealfinder.prescreen import prescreen
    from dealfinder.verticals import ART

    titles = [
        "Bernard Lennon Oil Portrait of Young Girl, Mid-20th Century",
        "Richard Stalter Farm Landscape Oil Painting, 20th Century",
        "Susan Grisell Plein Air Oil Painting of Grazing Horses",
        "Bernard Lennon Abstract Oil Painting",
    ]
    for title in titles:
        listing = RawListing(
            external_id="a", fb_listing_id="a", source="ebth", url="",
            title=title, description="",
        )
        result = prescreen(listing, ART, require_photo=False)
        assert result.score >= 2, f"{title!r} scored {result.score}: {result.reasons}"


def test_the_art_vertical_still_rejects_mass_market_reproductions():
    """The wider vocabulary must not open the door to the decor it was written to keep
    out — the negative list short-circuits before any positive term is counted."""
    from dealfinder.core.schemas import RawListing
    from dealfinder.prescreen import prescreen
    from dealfinder.verticals import ART

    for title in ("Framed Canvas Print Reproduction of a Landscape Painting",
                  "Hobby Lobby Wall Decor Abstract Painting Poster"):
        listing = RawListing(
            external_id="a", fb_listing_id="a", source="ebth", url="",
            title=title, description="",
        )
        assert not prescreen(listing, ART, require_photo=False).keep, title
