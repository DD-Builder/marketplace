"""The browser-interception fetch path, exercised without a real browser.

The point of the browser fetcher is that the app authenticates itself and fetches its
lots over GraphQL; we listen and capture those JSON payloads. These tests fake the
session (a plain object with ``fetch`` + ``drain_captures``) and prove the wiring:
captured payloads flow through the same shape-agnostic harvest as any embedded blob, so
an empty SPA shell plus intercepted JSON still yields fully-populated lots.
"""

from __future__ import annotations

import os

from dealfinder.sources.ebth import EbthClient, build_client, parse_page

# A realistic capture: the GraphQL response an EBTH search would return, using the field
# names the probe pulled from their bundle (highBidAmount, endsAt, aasmState).
_GQL_SEARCH = {
    "data": {"itemSearch": {"items": [
        {"id": "101-walnut-credenza", "title": "Mid Century Walnut Credenza",
         "highBidAmount": 65.0, "bidCount": 9, "endsAt": "2026-08-20T23:30:00Z",
         "url": "/items/101-walnut-credenza",
         "primaryImage": {"url": "https://cdn.ebth.com/a.jpg"}},
        {"id": "102-teak-lamp", "title": "Teak Table Lamp",
         "highBidAmount": 40.0, "bidCount": 3, "endsAt": "2026-08-19T20:00:00Z",
         "url": "/items/102-teak-lamp"},
    ]}}
}

_SHELL = "<html><body><div id='root'></div></body></html>"  # renders nothing itself


class FakeBrowser:
    """Stands in for BrowserSession: fetch returns the shell, drain_captures returns the
    JSON the 'app' fetched during that navigation."""

    def __init__(self, captures):
        self._next = captures
        self.calls: list[str] = []

    def fetch(self, url: str) -> str:
        self.calls.append(url)
        return _SHELL

    def drain_captures(self) -> list:
        caps, self._next = self._next, []
        return caps


def test_captured_graphql_json_becomes_lots_even_from_an_empty_shell():
    items = {i.item_id: i for i in parse_page(_SHELL, captures=[_GQL_SEARCH])}
    assert set(items) == {"101-walnut-credenza", "102-teak-lamp"}
    cred = items["101-walnut-credenza"]
    assert cred.current_bid_cents == 6500        # highBidAmount 65.0 -> cents
    assert cred.bid_count == 9
    assert cred.ends_at is not None
    assert cred.photo_urls == ["https://cdn.ebth.com/a.jpg"]


def test_the_client_folds_the_browsers_captures_into_search():
    browser = FakeBrowser([_GQL_SEARCH])
    client = EbthClient(fetch=browser.fetch, delay=0)
    items = {i.item_id: i for i in client.search("https://www.ebth.com/search?q=teak")}
    assert items["101-walnut-credenza"].current_bid_cents == 6500
    assert items["102-teak-lamp"].bid_count == 3
    assert browser.calls == ["https://www.ebth.com/search?q=teak"]


def test_an_http_fetch_with_no_capture_channel_still_works():
    """A plain callable (no drain_captures) must degrade to HTML-only parsing, so every
    existing test that injects `fetch=lambda` keeps working."""
    client = EbthClient(fetch=lambda url: _SHELL, delay=0)
    assert client.search("https://www.ebth.com/search?q=x") == []


def test_item_fetch_uses_captures_and_matches_the_url_slug():
    detail = {"data": {"item": {
        "id": "101-walnut-credenza", "title": "Walnut Credenza",
        "highBidAmount": 70.0, "bidCount": 11, "endsAt": "2026-08-20T23:30:00Z",
    }}}
    browser = FakeBrowser([detail])
    client = EbthClient(fetch=browser.fetch, delay=0)
    item = client.item("https://www.ebth.com/items/101-walnut-credenza")
    assert item is not None
    assert item.item_id == "101-walnut-credenza"
    assert item.current_bid_cents == 7000
    assert item.bid_count == 11


def test_build_client_honors_http_mode(monkeypatch):
    monkeypatch.setenv("EBTH_FETCH", "http")
    client = build_client()
    # HTTP client owns no browser -> close is a no-op, and the fetch is the urllib path.
    assert client._closer is None
    client.close()


def test_build_client_falls_back_to_http_when_browser_unavailable(monkeypatch):
    monkeypatch.setenv("EBTH_FETCH", "browser")
    # Simulate Playwright missing: the browser module raises on construction.
    import dealfinder.sources.ebth_browser as eb

    def boom(*a, **k):
        raise eb.PlaywrightUnavailable("no chromium")

    monkeypatch.setattr(eb, "BrowserSession", boom)
    client = build_client()
    assert client._closer is None            # fell back to HTTP rather than crashing
    client.close()


def test_probe_reports_captured_payload_structure():
    browser = FakeBrowser([_GQL_SEARCH])
    client = EbthClient(fetch=browser.fetch, delay=0)
    report = client.probe(["https://www.ebth.com/search?q=teak"])
    search = report["pages"][0]
    assert search["harvested_items"] == 2         # from the capture, not the shell
    assert "captured_payloads" in search
    payload = search["captured_payloads"][0]
    assert payload["harvested_items"] == 2
    assert payload["top_level_keys"] == ["data"]  # keys only — no values leaked
