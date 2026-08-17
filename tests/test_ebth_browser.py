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


def _search_response(items: list[dict], *, page: int, total_pages: int, total_items: int) -> dict:
    """Shaped like EBTH's real /browse?q= response (confirmed live: items + pages +
    applied_parameters), for pagination tests."""
    return {
        "items": items,
        "pages": {"current_page": page, "total_pages": total_pages,
                  "total_items": total_items, "items_per_page": len(items)},
        "applied_parameters": {"q": "teak", "page": page},
    }


class MultiPageBrowser:
    """Returns a different capture depending on the ``page`` query param — the shape
    EBTH's own /browse endpoint has (confirmed against the live site)."""

    def __init__(self, pages: dict[int, dict]):
        self.pages = pages
        self.calls: list[str] = []
        self._next: list = []

    def fetch(self, url: str) -> str:
        self.calls.append(url)
        import urllib.parse

        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        page = int((q.get("page") or ["1"])[0])
        self._next = [self.pages[page]] if page in self.pages else []
        return _SHELL

    def drain_captures(self) -> list:
        caps, self._next = self._next, []
        return caps


def _ebth_item(n: int) -> dict:
    return {"id": f"{n}-lot", "name": f"Lot {n}", "high_bid_amount": float(n),
            "bids_count": 1, "sale_ends_at": "2026-08-20T23:30:00Z", "aasm_state": "active"}


def test_search_pages_through_multiple_results_using_the_apis_own_page_count():
    browser = MultiPageBrowser({
        1: _search_response([_ebth_item(1), _ebth_item(2)], page=1, total_pages=3,
                            total_items=5),
        2: _search_response([_ebth_item(3), _ebth_item(4)], page=2, total_pages=3,
                            total_items=5),
        3: _search_response([_ebth_item(5)], page=3, total_pages=3, total_items=5),
    })
    client = EbthClient(fetch=browser.fetch, delay=0)
    items = client.search("https://www.ebth.com/browse?q=teak")
    assert {i.item_id for i in items} == {"1-lot", "2-lot", "3-lot", "4-lot", "5-lot"}
    assert browser.calls == [
        "https://www.ebth.com/browse?q=teak",
        "https://www.ebth.com/browse?q=teak&page=2",
        "https://www.ebth.com/browse?q=teak&page=3",
    ]


def test_search_respects_max_pages_and_does_not_crawl_the_whole_site():
    """A 129-page unfiltered browse must not turn one hourly run into 129 fetches."""
    browser = MultiPageBrowser({
        n: _search_response([_ebth_item(n)], page=n, total_pages=129, total_items=6148)
        for n in range(1, 130)
    })
    client = EbthClient(fetch=browser.fetch, delay=0)
    items = client.search("https://www.ebth.com/browse", max_pages=3)
    assert len(browser.calls) == 3
    assert len(items) == 3


def test_a_single_page_result_triggers_no_extra_fetches():
    browser = MultiPageBrowser({
        1: _search_response([_ebth_item(1)], page=1, total_pages=1, total_items=1),
    })
    client = EbthClient(fetch=browser.fetch, delay=0)
    client.search("https://www.ebth.com/browse?q=teak")
    assert browser.calls == ["https://www.ebth.com/browse?q=teak"]


def test_a_failed_page_does_not_lose_the_pages_already_fetched():
    class FlakyOnPage2(MultiPageBrowser):
        def fetch(self, url):
            if "page=2" in url:
                raise OSError("timeout")
            return super().fetch(url)

    browser = FlakyOnPage2({
        1: _search_response([_ebth_item(1)], page=1, total_pages=2, total_items=2),
        2: _search_response([_ebth_item(2)], page=2, total_pages=2, total_items=2),
    })
    client = EbthClient(fetch=browser.fetch, delay=0)
    items = client.search("https://www.ebth.com/browse?q=teak")
    assert {i.item_id for i in items} == {"1-lot"}


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
