"""The auction pipeline end-to-end, on a faked EBTH and a stubbed appraiser."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dealfinder import run_auctions
from dealfinder.auctions.catalog import load_auction_catalog
from dealfinder.core.schemas import AppraisalResult
from dealfinder.sources.ebth import EbthClient


def _page(items: list[dict]) -> str:
    links = "".join(f'<a href="/items/{i["id"]}">x</a>' for i in items)
    blob = json.dumps({"results": items})
    return (f"<html><body>{links}"
            f'<script id="__NEXT_DATA__" type="application/json">{blob}</script>'
            "</body></html>")


class Harness:
    def __init__(self, tmp_path, monkeypatch, items: list[dict]):
        self.tmp = tmp_path
        self.items = {i["id"]: dict(i) for i in items}
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
        monkeypatch.setenv("EBTH_SEARCH_URLS", "https://www.ebth.com/search?q=teak")
        self.fetches: list[str] = []

        def fetch(url: str) -> str:
            self.fetches.append(url)
            if "/search" in url:
                return _page(list(self.items.values()))
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            if slug not in self.items:
                import urllib.error

                raise urllib.error.HTTPError(url, 404, "gone", None, None)
            return _page([self.items[slug]])

        monkeypatch.setattr(run_auctions, "EbthClient",
                            lambda **kw: EbthClient(fetch=fetch, delay=0))

        class Stub:
            name = "stub"

            def appraise(self, listing, vertical, *, image_paths=None, comps=None):
                return AppraisalResult(
                    identified_item=f"appraised {listing.title}",
                    est_asis_value_cents=20000,
                    est_restored_resale_value_cents=60000,
                    est_restoration_cost_cents=4000,
                    est_restoration_effort_hours=3.0,
                    confidence=0.8, deal_score=55.0,
                )

        monkeypatch.setattr(run_auctions, "get_appraiser", lambda p: Stub())
        # No real photo downloads — the URLs are synthetic.
        monkeypatch.setattr(run_auctions, "_download_photos", lambda *a, **k: {})

    def run(self, *extra: str) -> int:
        return run_auctions.main([
            "--out", str(self.tmp / "site"),
            "--catalog", str(self.tmp / "site" / "catalog.json"),
            *extra,
        ])

    @property
    def catalog(self):
        return load_auction_catalog(self.tmp / "site" / "catalog.json")

    @property
    def status(self):
        return json.loads((self.tmp / "site" / "status.json").read_text())

    @property
    def page(self):
        return (self.tmp / "site" / "index.html").read_text()


def _lot(id="1-walnut-credenza", title="Mid Century Walnut Credenza", bid=25,
         ends_h=10.0, count=3):
    ends = (datetime.now(timezone.utc) + timedelta(hours=ends_h)).isoformat()
    return {"id": id, "title": title, "current_bid": bid, "bid_count": count,
            "ends_at": ends}


def test_a_run_discovers_watches_appraises_and_renders(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, [
        _lot(),                                                  # quality: walnut + mcm
        _lot(id="2-plastic-bin", title="Plastic Storage Bin", bid=5),   # junk: no signal
    ])
    assert h.run() == 0

    cat = h.catalog
    assert cat.lots["1-walnut-credenza"].watch
    assert not cat.lots["2-plastic-bin"].watch, "no positive signal — not watchlist material"
    assert cat.lots["1-walnut-credenza"].appraisal is not None
    assert cat.lots["2-plastic-bin"].appraisal is None, "appraisal budget is for the watchlist"

    page = h.page
    assert "Walnut Credenza" in page
    assert "Your max bid" in page
    assert h.status["state"] == "ok"
    assert h.status["actionable"] == 1        # inside 24h with headroom -> bid


def test_discovery_is_rationed_but_snapshots_are_not(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, [_lot()])
    assert h.run() == 0
    search_fetches = sum("/search" in u for u in h.fetches)
    h.fetches.clear()
    assert h.run() == 0                       # an hour later, effectively
    assert sum("/search" in u for u in h.fetches) == 0, "discovery not due yet"
    assert any("/items/" in u for u in h.fetches), "the endgame lot still gets snapshotted"
    assert search_fetches >= 1


def test_a_dead_site_renders_from_the_catalogue_and_says_so(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, [_lot()])
    assert h.run() == 0

    def broken(url):
        raise OSError("connection refused")

    monkeypatch.setattr(run_auctions, "EbthClient",
                        lambda **kw: EbthClient(fetch=broken, delay=0))
    # Force discovery to be due again so the failure is actually exercised.
    cat = h.catalog
    cat.last_discovery_at = datetime.now(timezone.utc) - timedelta(hours=48)
    from dealfinder.auctions.catalog import save_auction_catalog

    save_auction_catalog(cat, tmp_path / "site" / "catalog.json")

    assert h.run() == 5
    assert h.status["state"] == "scan_blocked"
    assert "could not be reached" in h.page


def test_a_404_marks_the_lot_gone(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, [_lot()])
    assert h.run() == 0
    del h.items["1-walnut-credenza"]          # the lot page starts 404ing
    assert h.run() == 0
    assert h.catalog.lots["1-walnut-credenza"].state == "gone"


def test_dry_run_tracks_but_never_calls_ai(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, [_lot()])

    def explode(p):
        raise AssertionError("dry run must not build an appraiser")

    monkeypatch.setattr(run_auctions, "get_appraiser", explode)
    assert h.run("--dry-run") == 0
    assert h.catalog.lots["1-walnut-credenza"].watch
    assert h.catalog.lots["1-walnut-credenza"].appraisal is None


def test_probe_writes_the_structure_report(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, [_lot()])
    assert h.run("--probe") == 0
    report = json.loads((tmp_path / "site" / "probe.json").read_text())
    assert report["pages"][0]["kind"] == "search"
    assert report["pages"][0]["harvested_items"] >= 1


def test_missing_token_in_ci_fails_before_any_spend(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, [_lot()])
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    assert h.run() == 3
    assert h.fetches == [], "credential failure must precede any fetch"
