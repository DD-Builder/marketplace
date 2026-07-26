"""Consecutive-run tests driving ``run_board.main()`` the way production does.

Every high-severity bug this project has shipped was a *second-run* bug — visible only
after a prior run left state behind: recovered data masquerading as fresh, a blocked
scan retiring listings, a dry run poisoning the catalogue, a price drop re-buying an
appraisal. A single-run test cannot see any of those, so this harness scripts a fetcher
day by day and asserts three things after every run: what was SPENT (stub call counts),
what was STORED (catalog.json), and what was SHOWN (index.html).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dealfinder import run_board
from dealfinder.core.schemas import AppraisalResult, RawListing, RawPhoto


def _listing(id_: str, price: int = 10000, *, title: str | None = None,
             desc: str = "", detail: bool = False, sold: bool | None = None) -> RawListing:
    return RawListing(
        fb_listing_id=id_,
        title=title or f"Walnut dresser {id_}",
        description=desc,
        asking_price_cents=price,
        location_text="Lexington, KY",
        url=f"https://www.facebook.com/marketplace/item/{id_}/",
        photos=[RawPhoto(remote_url=f"https://cdn.example/{id_}.jpg", position=0)],
        detail_fetched=detail,
        is_sold=sold,
    )


def _detailed(id_: str, price: int = 10000, **kw) -> RawListing:
    return _listing(id_, price, desc=f"solid walnut, dovetail drawers ({id_})",
                    detail=True, **kw)


class ScriptedFetcher:
    """Stands in for run_and_fetch. Each day is a dict:

    ``{"index": [listings] | Exception, "detail": [listings] | Exception}``
    An index call is any request with includeListingDetails=False; anything else
    (item URLs, single-stage retries) is served from "detail".
    """

    def __init__(self, days: list[dict]):
        self.days = days
        self.day = -1
        self.index_calls = 0
        self.detail_calls = 0

    def next_day(self) -> None:
        self.day += 1

    def __call__(self, run_input: dict, *, token: str = "", actor: str = ""):
        script = self.days[self.day]
        if run_input.get("includeListingDetails") is False:
            self.index_calls += 1
            resp = script["index"]
        else:
            self.detail_calls += 1
            resp = script.get("detail", [])
        if isinstance(resp, Exception):
            raise resp
        return list(resp)


class CountingProvider:
    name = "stub"

    def __init__(self):
        self.appraised: list[str] = []

    def appraise(self, listing, vertical, *, image_paths=None):
        self.appraised.append(listing.fb_listing_id)
        ask = listing.asking_price_cents or 0
        return AppraisalResult(
            identified_item=f"mid-century piece {listing.fb_listing_id}",
            est_asis_value_cents=ask + 5000,
            est_restored_resale_value_cents=ask * 3 + 40000,
            est_restoration_cost_cents=4000,
            est_restoration_effort_hours=3.0,
            confidence=0.8,
            deal_score=7.5,
            reasoning="stub appraisal",
        )


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch, days: list[dict]):
        self.tmp = tmp_path
        self.fetcher = ScriptedFetcher(days)
        self.provider = CountingProvider()
        monkeypatch.setenv("APIFY_TOKEN", "t")
        monkeypatch.setenv(
            "SEARCH_URLS",
            "https://www.facebook.com/marketplace/lexington/search/?query=dresser",
        )
        monkeypatch.setattr(run_board, "run_and_fetch", self.fetcher)
        monkeypatch.setattr(run_board, "get_appraiser", lambda name: self.provider)
        # Photos "download" instantly; saw-photos bookkeeping stays realistic.
        self.photo_calls: list[list[str]] = []

        def fake_photos(listings, out_dir, **kw):
            ids = [l.fb_listing_id for l in listings]
            self.photo_calls.append(ids)
            out_dir.mkdir(parents=True, exist_ok=True)
            got = {}
            for lid in ids:
                p = out_dir / f"{lid}_0.jpg"
                p.write_bytes(b"jpg")
                got[lid] = [p]
            return got

        monkeypatch.setattr(run_board, "_download_photos", fake_photos)

    def run(self, *extra: str) -> int:
        self.fetcher.next_day()
        return run_board.main([
            "--out", str(self.tmp / "site"),
            "--catalog", str(self.tmp / "catalog.json"),
            "--seen", str(self.tmp / "seen.json"),
            "--pieces", str(self.tmp / "pieces.json"),
            *extra,
        ])

    @property
    def catalog(self) -> dict:
        return json.loads((self.tmp / "catalog.json").read_text())

    @property
    def page(self) -> str:
        return (self.tmp / "site" / "index.html").read_text()


def test_day_two_price_drop_re_ranks_without_a_single_new_appraisal(tmp_path, monkeypatch):
    a, b = _detailed("a", 10000), _detailed("b", 20000)
    h = Harness(tmp_path, monkeypatch, days=[
        {"index": [a, b]},
        {"index": [_detailed("a", 4000), b]},   # a drops 60%
    ])
    assert h.run() == 0
    assert h.provider.appraised == ["a", "b"]

    assert h.run() == 0
    # The drop re-ranks for free: the appraisal describes the OBJECT, not the price.
    assert h.provider.appraised == ["a", "b"]          # no new calls
    assert h.catalog["listings"]["a"]["asking_price_cents"] == 4000
    assert "$40" in h.page


def test_a_quota_blocked_day_neither_hides_the_board_nor_fakes_absence(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, days=[
        {"index": [_detailed("a"), _detailed("b")]},
        {"index": RuntimeError("HTTP 403: Monthly usage hard limit exceeded")},
    ])
    assert h.run() == 0
    misses_before = {k: v["misses"] for k, v in h.catalog["listings"].items()}

    assert h.run() == 5                                 # failure IS reported...
    assert "Walnut dresser a" in h.page                 # ...but the board still renders
    after = h.catalog["listings"]
    assert {k: v["misses"] for k, v in after.items()} == misses_before
    assert all(v["state"] == "live" for v in after.values())


def test_a_failed_search_cannot_retire_listings_it_never_looked_for(tmp_path, monkeypatch):
    """Two searches; the dresser one starts failing while the credenza one keeps
    succeeding untruncated. The dresser listing takes misses, but nothing may be marked
    gone on the strength of a search that never ran."""
    dresser, credenza = _detailed("dr1"), _detailed("cr1")
    h = Harness(tmp_path, monkeypatch, days=[{}, {}, {}])
    monkeypatch.setenv(
        "SEARCH_URLS",
        "https://www.facebook.com/marketplace/lexington/search/?query=dresser\n"
        "https://www.facebook.com/marketplace/lexington/search/?query=credenza",
    )

    class TwoSearchFetcher(ScriptedFetcher):
        def __call__(self, run_input, *, token="", actor=""):
            if run_input.get("includeListingDetails") is False:
                self.index_calls += 1
                url = run_input["startUrls"][0]["url"]
                if "query=dresser" in url:
                    if self.day >= 1:
                        raise RuntimeError("HTTP 403: quota")
                    return [dresser]
                return [credenza]
            self.detail_calls += 1
            return []

    h.fetcher = TwoSearchFetcher([{}, {}, {}])
    monkeypatch.setattr(run_board, "run_and_fetch", h.fetcher)

    assert h.run() == 0
    assert h.run() == 0
    assert h.run() == 0
    entry = h.catalog["listings"]["dr1"]
    assert entry["misses"] >= 2                        # absence was honestly counted
    assert entry["state"] == "live"                    # but never treated as proof


def test_max_appraisals_is_a_hard_total_not_a_floor(tmp_path, monkeypatch):
    listings = [_detailed(f"x{i}", 10000 + i * 1000) for i in range(8)]
    h = Harness(tmp_path, monkeypatch, days=[{"index": listings}])
    assert h.run("--max-appraisals", "3", "--wildcards", "3") == 0
    # The old arithmetic spent top_n=max(3-3,1)=1 plus 3 wildcards = 4.
    assert len(h.provider.appraised) <= 3


def test_a_dry_run_poisons_nothing(tmp_path, monkeypatch):
    a = _detailed("a")
    h = Harness(tmp_path, monkeypatch, days=[{"index": [a]}, {"index": [a]}])
    assert h.run("--dry-run") == 0
    assert h.provider.appraised == []
    entry = h.catalog["listings"]["a"]
    assert entry["appraisal"] is None                   # the stub result was NOT stored

    assert h.run() == 0
    assert h.provider.appraised == ["a"]                # the real run still pays once


def test_a_blind_valuation_is_redone_when_photos_arrive(tmp_path, monkeypatch):
    a = _detailed("a")
    h = Harness(tmp_path, monkeypatch, days=[{"index": [a]}, {"index": [a]}])
    assert h.run("--no-photos") == 0
    assert h.provider.appraised == ["a"]
    assert h.catalog["listings"]["a"]["appraised_with_photos"] is False

    assert h.run() == 0                                 # photos now available
    assert h.provider.appraised == ["a", "a"]           # exactly one redo
    assert h.catalog["listings"]["a"]["appraised_with_photos"] is True


def test_the_redo_happens_once_not_every_run(tmp_path, monkeypatch):
    a = _detailed("a")
    h = Harness(tmp_path, monkeypatch, days=[{"index": [a]}] * 3)
    assert h.run("--no-photos") == 0
    assert h.run() == 0
    assert h.run() == 0
    assert h.provider.appraised == ["a", "a"]           # blind, redo, then never again


def test_a_corrupt_catalogue_aborts_with_a_backup_instead_of_being_replaced(
    tmp_path, monkeypatch
):
    h = Harness(tmp_path, monkeypatch, days=[{"index": [_detailed("a")]}])
    assert h.run() == 0
    good = (tmp_path / "catalog.json").read_text()

    (tmp_path / "catalog.json").write_text(good[: len(good) // 2])  # truncated write
    h2 = Harness(tmp_path, monkeypatch, days=[{"index": [_detailed("a")]}])
    assert h2.run() == 6
    assert (tmp_path / "catalog.json").read_text() == good[: len(good) // 2]  # untouched
    assert list(tmp_path.glob("catalog.json.corrupt-*"))
