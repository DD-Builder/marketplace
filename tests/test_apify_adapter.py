"""Apify adapter tests — against a record shaped like the real actor export."""

from __future__ import annotations

from dealfinder.sources.apify import record_to_listing, records_to_listings

# A record mirroring the real apify/facebook-marketplace-scraper schema (nested objects).
REAL_SHAPE = {
    "id": "1833252411415719",
    "itemUrl": "https://www.facebook.com/marketplace/item/1833252411415719/",
    "listingTitle": "Dresser",
    "description": {"text": "6 drawer dresser, solid oak"},
    "locationText": {"text": "Richmond, KY"},
    "location": {"latitude": 37.7, "longitude": -84.2},
    "listingPrice": {"amount_with_offset_in_currency": "8000", "amount": "80.00", "currency": "USD"},
    "strikethroughPrice": {"amount": "100.00"},
    "listingPhotos": [
        {"image": {"uri": "https://scontent.example/a.jpg", "height": 960, "width": 720}},
        {"image": {"uri": "https://scontent.example/b.jpg"}},
    ],
    "primaryListingPhoto": {"photo_image_url": "https://scontent.example/thumb.jpg"},
}


def test_adapter_reads_nested_real_fields():
    r = record_to_listing(REAL_SHAPE)
    assert r.fb_listing_id == "1833252411415719"
    assert r.title == "Dresser"
    assert r.description == "6 drawer dresser, solid oak"
    assert r.location_text == "Richmond, KY"
    assert r.asking_price_cents == 8000  # amount_with_offset_in_currency, already cents
    assert r.url.endswith("/1833252411415719/")
    assert [p.remote_url for p in r.photos] == [
        "https://scontent.example/a.jpg", "https://scontent.example/b.jpg"
    ]


def test_adapter_captures_price_drop_marker():
    r = record_to_listing(REAL_SHAPE)
    assert r.raw_json["_was_price_cents"] == 10000  # strikethrough $100 -> was 10000 cents


def test_adapter_falls_back_to_primary_photo():
    rec = dict(REAL_SHAPE)
    rec["listingPhotos"] = []
    r = record_to_listing(rec)
    assert r.photos and r.photos[0].remote_url.endswith("/thumb.jpg")


def test_records_without_an_id_are_dropped_not_renumbered():
    """Ids are the catalogue's primary keys and the photo filenames. The old fallback
    (`row-{idx}`) named a *different* listing on every run, so entries silently inherited
    each other's appraisals. No id -> not a listing we can track -> dropped."""
    out = records_to_listings(
        [{"listingTitle": "x"}, {"id": "123", "listingTitle": "y"}, {"listingTitle": "z"}]
    )
    assert [l.fb_listing_id for l in out] == ["123"]
