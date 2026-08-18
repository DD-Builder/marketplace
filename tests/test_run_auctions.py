"""The auction pipeline end-to-end, on a faked EBTH and a stubbed appraiser."""

from __future__ import annotations

import json
import urllib.parse
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

        # main() builds its client through _make_client; inject the fake there so no
        # browser is ever launched under test.
        self._fetch = fetch
        monkeypatch.setattr(run_auctions, "_make_client",
                            lambda: EbthClient(fetch=fetch, delay=0))

        # Every AI valuation this pipeline would pay for, recorded by lot id. Valuation
        # is the only metered step in a run, so tests can assert what it cost.
        self.appraise_calls: list[str] = []
        outer = self

        class Stub:
            name = "stub"

            def appraise(self, listing, vertical, *, image_paths=None, comps=None):
                outer.appraise_calls.append(listing.fb_listing_id)
                # $900 as-is: worth the round trip to Cincinnati even as a bulky lot.
                # (The as-is figure is what the bid math prices off now — nothing here
                # is restored — so it has to clear the pickup cost on its own.)
                return AppraisalResult(
                    identified_item=f"appraised {listing.title}",
                    est_asis_value_cents=90000,
                    est_restored_resale_value_cents=120000,
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


def test_search_runs_every_run_and_refreshes_live_bids(tmp_path, monkeypatch):
    """EBTH's search payload carries every lot's live bid, so one search per run is both
    discovery and snapshot — the endgame gets fresh bids hourly without item-page fetches."""
    h = Harness(tmp_path, monkeypatch, [_lot(bid=25, count=3)])
    assert h.run() == 0
    assert h.catalog.lots["1-walnut-credenza"].current_bid_cents == 2500

    # The bidding moves; next run's search must pick it up with no item-page fetch.
    h.items["1-walnut-credenza"]["current_bid"] = 80
    h.items["1-walnut-credenza"]["bid_count"] = 7
    h.fetches.clear()
    assert h.run() == 0
    assert sum("/search" in u for u in h.fetches) >= 1, "search runs every run"
    assert not any("/items/" in u for u in h.fetches), \
        "a lot still in search results needs no item-page fetch"
    entry = h.catalog.lots["1-walnut-credenza"]
    assert entry.current_bid_cents == 8000 and entry.bid_count == 7
    # And the move was recorded as a distinct point in the bid history.
    assert [p.bid_cents for p in entry.bid_history] == [2500, 8000]


def test_a_dead_site_renders_from_the_catalogue_and_says_so(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, [_lot()])
    assert h.run() == 0

    def broken(url):
        raise OSError("connection refused")

    monkeypatch.setattr(run_auctions, "_make_client",
                        lambda: EbthClient(fetch=broken, delay=0))
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


# --- multi-vertical discovery -----------------------------------------------------------
# EBTH sells far more than furniture — jewelry, silver, coins, watches, rugs, fine art —
# and an earlier version of this pipeline only ever searched two furniture queries, then
# screened every discovered lot (regardless of category) through furniture's own keyword
# gate. A jewelry lot has no "walnut" or "Lane" in it, so it silently never made the
# watchlist. These tests pin the fix: discovery spans every known vertical, and each lot
# is screened/appraised under the vertical whose query actually found it.

def test_search_targets_default_to_every_known_vertical_not_just_furniture():
    targets = run_auctions._search_targets("", default_vertical="furniture")
    keyword_targets = targets[:len(run_auctions._DEFAULT_QUERIES)]
    assert keyword_targets == [
        (vkey, f"https://www.ebth.com/browse?q={urllib.parse.quote(q)}")
        for vkey, q in run_auctions._DEFAULT_QUERIES
    ]
    verticals_covered = {v for v, _ in keyword_targets}
    assert verticals_covered == {"furniture", "art", "electronics", "jewelry", "collectibles"}


def test_search_targets_always_include_ending_soon_and_recommended():
    """A keyword search can only ever find a lot that happens to contain a guessed
    word. These two site-wide sources are the fix — confirmed live: sort=sale_ends_at_
    asc is EBTH's real 'Ending Soonest' preset, and days_left genuinely narrows the
    result set (1242/2017/3200 items at days_left=1/2/3, site-wide)."""
    targets = run_auctions._search_targets("", default_vertical="furniture")
    by_vertical = dict(targets)
    assert by_vertical[run_auctions._AUTO_ENDING_SOON] == (
        "https://www.ebth.com/browse?sort=sale_ends_at_asc&days_left=2"
    )
    assert by_vertical[run_auctions._AUTO_RECOMMENDED] == (
        "https://www.ebth.com/browse?sort=recommended"
    )


def test_time_critical_days_is_configurable(monkeypatch):
    monkeypatch.setenv("EBTH_TIME_CRITICAL_DAYS", "5")
    targets = run_auctions._search_targets("", default_vertical="furniture")
    by_vertical = dict(targets)
    assert "days_left=5" in by_vertical[run_auctions._AUTO_ENDING_SOON]


def test_search_targets_honors_an_explicit_override_under_one_vertical():
    """The documented, backward-compatible shape: user-supplied URLs are plain URLs,
    all screened under whatever --vertical/VERTICAL says. The two site-wide sources
    are appended even here — they exist to catch what keyword search structurally
    can't, regardless of which keywords the user chose."""
    targets = run_auctions._search_targets(
        "https://www.ebth.com/browse?q=teak\nhttps://www.ebth.com/browse?q=oak",
        default_vertical="furniture",
    )
    assert targets[:2] == [
        ("furniture", "https://www.ebth.com/browse?q=teak"),
        ("furniture", "https://www.ebth.com/browse?q=oak"),
    ]
    assert {v for v, _ in targets[2:]} == {
        run_auctions._AUTO_ENDING_SOON, run_auctions._AUTO_RECOMMENDED,
    }


def test_watchlist_screens_each_lot_under_its_own_discovered_vertical():
    """The core regression, isolated from the pipeline: a jewelry lot must be screened
    by jewelry's rules, and would be silently dropped forever under furniture's."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry
    from dealfinder.verticals import FURNITURE, JEWELRY
    from dealfinder.prescreen import prescreen

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    cat.lots["j-1"] = AuctionEntry(
        id="j-1", title="Sterling Silver Diamond Ring",
        description="14k gold band, sterling silver diamond setting",
        vertical="jewelry", state="live",
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=10),
    )
    # Proves the regression directly: this listing fails furniture's gate (no signal,
    # no photo) and passes jewelry's (positive keywords: sterling, diamond, 14k, gold).
    assert not prescreen(cat.lots["j-1"].to_listing(), FURNITURE, require_photo=False).keep
    assert prescreen(cat.lots["j-1"].to_listing(), JEWELRY, require_photo=False).keep

    promoted = run_auctions._refresh_watchlist(cat, "furniture", cap=10)
    assert promoted == 1
    assert cat.lots["j-1"].watch


def test_watchlist_falls_back_to_the_default_vertical_for_untagged_legacy_entries():
    """Entries from before `vertical` existed have vertical="" and must not crash or
    silently vanish from screening — they fall back to the run's default."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    cat.lots["f-1"] = AuctionEntry(
        id="f-1", title="Danish Teak Sideboard", description="solid teak, mid century",
        vertical="", state="live", first_seen=now, last_seen=now,
        ends_at=now + timedelta(hours=10),
    )
    promoted = run_auctions._refresh_watchlist(cat, "furniture", cap=10)
    assert promoted == 1
    assert cat.lots["f-1"].watch


def test_ending_soon_lots_are_watched_with_no_keyword_signal_at_all():
    """The user's own framing: 'even if we just review items closing in 2 days or
    less' — a lot with zero furniture/art/jewelry/etc. keywords must still be watched
    when it was discovered by the ending-soon source, because the urgency itself is
    the reason to look, not a guessed word matching its description."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    cat.lots["x-1"] = AuctionEntry(
        id="x-1", title="Miscellaneous Household Lot", description="assorted items",
        vertical=run_auctions._AUTO_ENDING_SOON, state="ending",
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=5),
    )
    # Confirms the premise: this listing has no positive signal in any vertical.
    from dealfinder.prescreen import prescreen
    from dealfinder.verticals import FURNITURE
    assert not prescreen(cat.lots["x-1"].to_listing(), FURNITURE, require_photo=False).keep

    promoted = run_auctions._refresh_watchlist(cat, "furniture", cap=10)
    assert promoted == 1
    assert cat.lots["x-1"].watch
    # Reclassified for pricing/appraisal purposes, not left as the raw sentinel.
    assert cat.lots["x-1"].vertical != run_auctions._AUTO_ENDING_SOON


def test_ending_soon_lots_win_the_cap_over_ordinary_keyword_matches():
    """Urgency always gets first claim on watchlist room — that's the entire point of
    an auction tracker: the endgame is the only window where bidding pays."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    for i in range(5):
        cat.lots[f"k-{i}"] = AuctionEntry(
            id=f"k-{i}", title="Danish Teak Sideboard", description="solid teak",
            vertical="furniture", state="live",
            first_seen=now, last_seen=now, ends_at=now + timedelta(days=5),
        )
    cat.lots["urgent"] = AuctionEntry(
        id="urgent", title="Random Lot", description="no signal here",
        vertical=run_auctions._AUTO_ENDING_SOON, state="ending",
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=1),
    )
    promoted = run_auctions._refresh_watchlist(cat, "furniture", cap=1)
    assert promoted == 1
    assert cat.lots["urgent"].watch
    assert not any(cat.lots[f"k-{i}"].watch for i in range(5))


def test_recommended_lots_compete_normally_rather_than_dominating():
    """Unlike ending-soon, 'recommended' isn't time-critical — it shouldn't crowd out
    a real keyword match, just get a fair shot at remaining room."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    cat.lots["strong-match"] = AuctionEntry(
        id="strong-match", title="Sterling Silver Tiffany Bracelet",
        description="hallmarked, 14k gold clasp", vertical="jewelry", state="live",
        first_seen=now, last_seen=now, ends_at=now + timedelta(days=3),
    )
    cat.lots["reco"] = AuctionEntry(
        id="reco", title="Recommended Pick", description="no keywords here",
        vertical=run_auctions._AUTO_RECOMMENDED, state="live",
        first_seen=now, last_seen=now, ends_at=now + timedelta(days=3),
    )
    promoted = run_auctions._refresh_watchlist(cat, "furniture", cap=1)
    assert promoted == 1
    # The strong keyword match (jewelry: sterling, hallmarked, 14k, gold, Tiffany maker)
    # outscores the flat recommended-priority of 1, so it wins the single slot.
    assert cat.lots["strong-match"].watch
    assert not cat.lots["reco"].watch


def test_best_vertical_classifies_by_content_not_discovery_source():
    from dealfinder.auctions.catalog import AuctionEntry

    now = datetime.now(timezone.utc)
    entry = AuctionEntry(
        id="j-2", title="14k Gold Diamond Engagement Ring", description="sterling",
        vertical=run_auctions._AUTO_ENDING_SOON, state="live",
        first_seen=now, last_seen=now,
    )
    assert run_auctions._best_vertical(entry) == "jewelry"


def test_a_default_run_discovers_and_watches_a_jewelry_lot_end_to_end(tmp_path, monkeypatch):
    """Full pipeline, dry-run (no AI spend): every default vertical's query actually
    fires, and a jewelry-flavored result gets tagged and watched — not silently lost."""
    from dealfinder.sources.ebth import AuctionItem

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
    monkeypatch.delenv("EBTH_SEARCH_URLS", raising=False)
    seen_queries: list[str] = []

    def fake_search(self, url, **kw):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]
        seen_queries.append(q)
        if "silver" in q or "diamond" in q:
            return [AuctionItem(
                item_id="j-1", title="Sterling Silver Diamond Ring",
                description="14k gold band, sterling silver diamond setting",
                current_bid_cents=5000, bid_count=2,
                ends_at=datetime.now(timezone.utc) + timedelta(hours=10),
                url="https://www.ebth.com/items/j-1",
            )]
        return []

    monkeypatch.setattr(EbthClient, "search", fake_search)
    monkeypatch.setattr(run_auctions, "_make_client",
                        lambda: EbthClient(fetch=lambda u: "<html></html>", delay=0))

    rc = run_auctions.main([
        "--out", str(tmp_path / "site"),
        "--catalog", str(tmp_path / "site" / "catalog.json"),
        "--dry-run",
    ])
    assert rc == 0
    # +2: the always-on ending-soon/recommended site-wide sources, alongside every
    # per-vertical keyword query — not just furniture's.
    assert len(seen_queries) == len(run_auctions._DEFAULT_QUERIES) + 2

    cat = load_auction_catalog(tmp_path / "site" / "catalog.json")
    entry = cat.lots["j-1"]
    assert entry.vertical == "jewelry"
    assert entry.watch, "a jewelry lot must be watched under its own vertical's rules"


# --- cost control: one valuation per lot, ever -------------------------------------------
# Valuation is the only metered step in the whole pipeline. Everything else — bid
# refreshes, projections, max-bid arithmetic, the rendered page — is pure computation on
# a stored appraisal. An hourly job that re-valued its watchlist would cost ~24x what
# this one does and tell you nothing new, since what an object *is* doesn't change when
# somebody raises the bid on it.

def test_a_lot_is_valued_once_and_never_again(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, [_lot()])
    assert h.run() == 0
    assert h.appraise_calls == ["1-walnut-credenza"], "first run pays for the valuation"

    for _ in range(3):
        assert h.run() == 0
    assert h.appraise_calls == ["1-walnut-credenza"], \
        "later runs must reuse the stored appraisal, not re-buy it"


def test_a_rising_bid_updates_the_advice_without_a_single_ai_call(tmp_path, monkeypatch):
    """The property the whole hourly cadence rests on: bids move constantly, valuations
    never do, so tracking the endgame has to be free."""
    h = Harness(tmp_path, monkeypatch, [_lot(bid=25)])
    assert h.run() == 0
    first = h.catalog.lots["1-walnut-credenza"]
    assert first.current_bid_cents == 2500
    calls_after_valuation = len(h.appraise_calls)

    # The bidding climbs across the next three hourly runs.
    for bid in (150, 400, 700):
        h.items["1-walnut-credenza"]["current_bid"] = bid
        assert h.run() == 0

    entry = h.catalog.lots["1-walnut-credenza"]
    assert entry.current_bid_cents == 70000, "the bid tracked all the way up"
    assert [p.bid_cents for p in entry.bid_history] == [2500, 15000, 40000, 70000]
    assert len(h.appraise_calls) == calls_after_valuation, \
        "not one extra valuation was bought while the price moved"

    # And the advice genuinely moved with the price: what was worth bidding on at $25
    # is priced out at $700 against a $900 as-is value.
    assert h.status["actionable"] == 0


def test_the_appraiser_is_never_even_constructed_when_nothing_needs_valuing(
    tmp_path, monkeypatch
):
    """Not just 'no calls' — no provider, so a run with a full watchlist can't fail on a
    missing credential or spend a token by accident."""
    h = Harness(tmp_path, monkeypatch, [_lot()])
    assert h.run() == 0                     # values the lot

    def explode(provider):
        raise AssertionError("built an appraiser with nothing left to appraise")

    monkeypatch.setattr(run_auctions, "get_appraiser", explode)
    assert h.run() == 0
