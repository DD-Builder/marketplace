"""EBTH extraction layers, exercised on synthetic pages.

The real site is unreachable from the dev environment, so these tests pin the *contract*
of each layer — JSON-LD, shape-agnostic embedded JSON, link discovery — against pages
built to that layer's public shape. The CI probe validates the shapes against reality;
these validate that whatever shape holds, the layers extract it correctly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from dealfinder.sources.ebth import (
    AuctionItem,
    EbthClient,
    harvest_json,
    harvest_jsonld,
    item_id_from_url,
    item_links,
    parse_money_cents,
    parse_page,
    parse_when,
)


# --- coercion ---------------------------------------------------------------------------

def test_money_strings_and_floats_are_dollars():
    assert parse_money_cents("$1,234.50") == 123450
    assert parse_money_cents("45") == 4500
    assert parse_money_cents(45.0) == 4500


def test_money_ints_are_dollars_unless_the_key_says_cents():
    assert parse_money_cents(45, "current_bid") == 4500
    assert parse_money_cents(4500, "current_bid_cents") == 4500


def test_money_garbage_is_none_not_zero():
    # None must stay distinguishable from "free" — the board renders them differently.
    assert parse_money_cents(None) is None
    assert parse_money_cents("call for price") is None
    assert parse_money_cents(True) is None


def test_when_handles_iso_z_and_epochs():
    iso = parse_when("2026-08-20T17:00:00Z")
    assert iso == datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)
    assert parse_when(iso.timestamp()) == iso
    assert parse_when(iso.timestamp() * 1000) == iso
    assert parse_when("soon") is None


# --- layer 1: JSON-LD -------------------------------------------------------------------

_JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Mid Century Walnut Credenza",
 "url":"https://www.ebth.com/items/12345-walnut-credenza",
 "image":["https://cdn.ebth.com/a.jpg"],
 "description":"A long low walnut credenza.",
 "offers":{"@type":"Offer","price":"85.00","priceCurrency":"USD",
           "availabilityEnds":"2026-08-19T23:30:00Z"}}
</script>
</head><body></body></html>
"""


def test_jsonld_product_becomes_an_auction_item():
    items = harvest_jsonld(_JSONLD_PAGE)
    assert len(items) == 1
    it = items[0]
    assert it.item_id == "12345-walnut-credenza"
    assert it.current_bid_cents == 8500
    assert it.ends_at == datetime(2026, 8, 19, 23, 30, tzinfo=timezone.utc)
    assert it.photo_urls == ["https://cdn.ebth.com/a.jpg"]
    assert it.parsed_by == "json-ld"


# --- layer 2: shape-agnostic embedded JSON ----------------------------------------------

def test_harvest_finds_lots_whatever_the_nesting():
    blob = {
        "props": {"pageProps": {"results": [
            {"id": 987, "title": "Lane End Table", "current_bid": 12,
             "bid_count": 3, "ends_at": "2026-08-18T22:00:00Z",
             "url": "/items/987-lane-end-table",
             "image_url": "https://cdn.ebth.com/b.jpg"},
            {"id": 988, "name": "Brass Lamp", "currentBid": 40.5,
             "endsAt": 1787392800},
        ]}},
        "unrelated": {"id": "nav", "label": "menu"},   # no bid/end fields -> ignored
    }
    items = {i.item_id: i for i in harvest_json(blob)}
    assert set(items) == {"987", "988"}
    assert items["987"].current_bid_cents == 1200
    assert items["987"].bid_count == 3
    assert items["987"].url == "https://www.ebth.com/items/987-lane-end-table"
    assert items["988"].current_bid_cents == 4050
    assert items["988"].ends_at is not None


def test_shards_of_the_same_lot_merge_instead_of_duplicating():
    blob = [
        {"id": "55-chair", "title": "Chair", "current_bid": 10},
        {"id": "55-chair", "ends_at": "2026-08-18T22:00:00Z", "bid_count": 7},
    ]
    items = harvest_json(blob)
    assert len(items) == 1
    assert items[0].current_bid_cents == 1000
    assert items[0].bid_count == 7
    assert items[0].ends_at is not None


def test_cents_named_fields_are_not_multiplied_again():
    items = harvest_json({"id": "9", "current_bid_cents": 12550})
    assert items[0].current_bid_cents == 12550


# --- layer 3 + assembly -----------------------------------------------------------------

_SEARCH_PAGE = f"""
<html><body>
<a href="/items/111-teak-dresser">Teak Dresser</a>
<a href="/items/111-teak-dresser">dup link</a>
<a href="https://www.ebth.com/items/222-oak-desk">Oak Desk</a>
<a href="/help/faq">not an item</a>
<script id="__NEXT_DATA__" type="application/json">
{json.dumps({"props": {"items": [
    {"id": "111-teak-dresser", "title": "Teak Dresser", "current_bid": 25,
     "ends_at": "2026-08-18T20:00:00Z"}]}})}
</script>
</body></html>
"""


def test_a_search_page_yields_harvested_items_plus_link_only_stubs():
    links = item_links(_SEARCH_PAGE)
    assert links == [
        "https://www.ebth.com/items/111-teak-dresser",
        "https://www.ebth.com/items/222-oak-desk",
    ]
    client = EbthClient(fetch=lambda url: _SEARCH_PAGE, delay=0)
    items = {i.item_id: i for i in client.search("https://www.ebth.com/search?q=teak")}
    assert items["111-teak-dresser"].current_bid_cents == 2500
    # 222 appeared only as a link — still tracked, to be filled by an item fetch.
    assert items["222-oak-desk"].parsed_by == "link-only"


def test_item_fetch_prefers_the_record_matching_the_url():
    page = f"""
    <script id="__NEXT_DATA__" type="application/json">
    {json.dumps({"item": {"id": "333-rocker", "title": "Rocker", "current_bid": 30,
                          "bid_count": 4, "ends_at": "2026-08-18T20:00:00Z"},
                 "related": [{"id": "999-lamp", "current_bid": 5}]})}
    </script>"""
    client = EbthClient(fetch=lambda url: page, delay=0)
    item = client.item("https://www.ebth.com/items/333-rocker")
    assert item is not None
    assert item.item_id == "333-rocker"
    assert item.bid_count == 4


def test_window_state_assignment_is_parsed_too():
    page = ('<script>window.__INITIAL_STATE__ = {"lots": [{"id": "7-desk", '
            '"current_bid": 15, "ends_at": "2026-08-18T20:00:00Z"}]};</script>')
    items = parse_page(page)
    assert [i.item_id for i in items] == ["7-desk"]


def test_item_id_from_url_is_the_slug():
    assert item_id_from_url("https://www.ebth.com/items/12345-walnut-credenza/") == \
        "12345-walnut-credenza"


# --- probe ------------------------------------------------------------------------------

def test_probe_reports_structure_not_content():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return _SEARCH_PAGE

    client = EbthClient(fetch=fake_fetch, delay=0)
    report = client.probe(["https://www.ebth.com/search?q=teak"])
    kinds = [p["kind"] for p in report["pages"]]
    assert kinds == ["search", "item"]          # follows one discovered item link
    search_page = report["pages"][0]
    assert search_page["harvested_items"] >= 1
    assert search_page["field_coverage"]["current_bid_cents"] >= 1
    # Raw keys only — never values — so the committed report can't leak page content.
    assert all(isinstance(k, str) for k in search_page["sample_raw_keys"])


def test_probe_records_failures_instead_of_raising():
    def broken(url):
        raise OSError("blocked by network egress proxy")

    client = EbthClient(fetch=broken, delay=0)
    report = client.probe(["https://www.ebth.com/search?q=x"])
    assert "OSError" in report["pages"][0]["error"]
