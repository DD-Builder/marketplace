"""Pre-screen heuristic tests."""

from __future__ import annotations

from dealfinder.core.schemas import RawListing, RawPhoto
from dealfinder.prescreen import prescreen


def _listing(title="", desc="", price=5000, photos=1):
    return RawListing(
        fb_listing_id="x",
        title=title,
        description=desc,
        asking_price_cents=price,
        photos=[RawPhoto(remote_url="u", position=0)] * photos,
    )


def test_rejects_negative_keywords():
    r = prescreen(_listing(title="IKEA Malm", desc="particle board"))
    assert not r.keep


def test_rejects_no_photos():
    assert not prescreen(_listing(title="teak sideboard", photos=0)).keep


def test_keeps_positive_signal():
    r = prescreen(_listing(title="Vintage teak Danish sideboard"))
    assert r.keep and r.score >= 1


def test_maker_scores_higher_than_generic():
    generic = prescreen(_listing(title="oak table")).score
    maker = prescreen(_listing(title="Herman Miller oak table")).score
    assert maker > generic


def test_keeps_mistitled_piece_for_vision():
    # No keywords, but has a photo and a real price -> keep so vision can catch it.
    r = prescreen(_listing(title="Old dresser", desc="needs work"))
    assert r.keep


def test_rejects_out_of_range_price():
    assert not prescreen(_listing(title="teak", price=5_000_000)).keep
