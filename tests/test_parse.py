"""Parser tests against saved Facebook markup fixtures.

These guard the single highest-fragility component: when Facebook changes its embedded
JSON shape, these fail first and point at exactly what broke.
"""

from __future__ import annotations

import pytest

from dealfinder.scraper import parse
from dealfinder.scraper.errors import LayoutChangedError
from tests.conftest import load_fixture


def test_parse_listing_detail_extracts_fields():
    html = load_fixture("listing_detail.html")
    listing = parse.parse_listing_detail(html, url="https://fb.com/marketplace/item/100200300400/")

    assert listing.fb_listing_id == "100200300400"
    assert listing.title == "Vintage teak sideboard"
    assert listing.asking_price_cents == 12000  # amount_with_offset wins
    assert listing.currency == "USD"
    assert "teak" in listing.description.lower()
    assert listing.location_text == "Brooklyn, NY"
    assert listing.seller_name == "Jane D."
    assert listing.seller_profile_url and "555000111" in listing.seller_profile_url
    assert len(listing.photos) == 2
    assert listing.photos[0].remote_url.endswith("photo1.jpg")


def test_parse_search_ids_finds_all_cards():
    html = load_fixture("search_page.html")
    ids = parse.parse_search_ids(html)
    assert ids == ["111", "222", "333"]


def test_parse_listing_detail_raises_on_unknown_layout():
    html = "<html><body><script type=\"application/json\">{\"nope\":1}</script></body></html>"
    with pytest.raises(LayoutChangedError):
        parse.parse_listing_detail(html)


def test_price_falls_back_to_amount_when_no_offset():
    html = (
        '<script type="application/json">'
        '{"target":{"id":"9","marketplace_listing_title":"X",'
        '"listing_price":{"amount":"1,250","currency":"USD"}}}'
        "</script>"
    )
    listing = parse.parse_listing_detail(html)
    assert listing.asking_price_cents == 125000
