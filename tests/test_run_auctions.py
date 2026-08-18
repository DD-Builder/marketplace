"""The auction pipeline end-to-end, on a faked EBTH and a stubbed appraiser."""

from __future__ import annotations

import collections
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

def test_discovery_walks_ebths_own_categories_not_guessed_keywords():
    """A keyword query can only find a lot whose text happens to contain the word.
    category_id returns everything the house itself filed there — including the lot
    titled "Estate Lot, Assorted" that no keyword would ever surface."""
    from dealfinder.auctions.categories import CATEGORIES, SORTS

    targets = run_auctions._search_targets("", default_vertical="furniture")
    urls = [u for _v, u in targets]
    assert all("q=" not in u for u in urls), "no keyword queries left in discovery"

    # Every category we can honestly price is trawled, under its own vertical.
    on = [c for c in CATEGORIES if c.default_on]
    for cat in on:
        mine = [(v, u) for v, u in targets if f"category_id={cat.id}" in u]
        assert mine, f"{cat.label} is never searched"
        assert all(v == cat.vertical for v, _ in mine), \
            f"{cat.label} must be priced as {cat.vertical}"

    # Jewelry and Watches is a single EBTH category — exactly the ask.
    jw = next(c for c in CATEGORIES if c.label == "Jewelry and Watches")
    assert jw.vertical == "jewelry"
    assert any(f"category_id={jw.id}" in u for u in urls)

    # And each one is pulled under EBTH's own sort presets, not ours.
    for key in ("ending_soon", "highest_bid", "most_bids"):
        assert any(f"sort={SORTS[key]}" in u for u in urls), key


def test_discovery_is_narrowed_to_the_decision_window():
    """Valuing a lot that closes next week buys nothing; every category pull is already
    filtered to what's actually decidable."""
    targets = run_auctions._search_targets("", default_vertical="furniture")
    cat_urls = [u for _v, u in targets if "category_id=" in u]
    assert cat_urls and all("days_left=2" in u for u in cat_urls)


def test_categories_can_be_chosen_by_name_id_or_slug(monkeypatch):
    """So a repo variable can say "Jewelry and Watches, Furniture" in EBTH's own words."""
    from dealfinder.auctions.categories import resolve

    by_name = resolve("Jewelry and Watches, Furniture")
    assert [c.slug for c in by_name] == ["jewelry-and-watches", "furniture"]
    assert [c.id for c in resolve("3313,3472")] == [3313, 3472]
    assert [c.slug for c in resolve("jewelry-and-watches")] == ["jewelry-and-watches"]
    # A typo narrows the trawl; it must never break the hourly run.
    assert resolve("Jewelry and Watches, nonsense-category")[0].id == 3313

    monkeypatch.setenv("EBTH_CATEGORIES", "Jewelry and Watches")
    targets = run_auctions._search_targets("", default_vertical="furniture")
    cat_urls = [u for _v, u in targets if "category_id=" in u]
    assert cat_urls and all("category_id=3313" in u for u in cat_urls)


def test_uncertain_categories_are_listed_but_not_trawled_by_default():
    """Appliances and vehicles stay selectable so the taxonomy is complete, but an
    appraisal we can't stand behind is worse than no appraisal."""
    from dealfinder.auctions.categories import BY_SLUG

    assert not BY_SLUG["appliances"].default_on
    assert not BY_SLUG["automotive"].default_on
    urls = [u for _v, u in run_auctions._search_targets("", default_vertical="furniture")]
    assert not any(f"category_id={BY_SLUG['appliances'].id}" in u for u in urls)


def test_explicit_urls_still_override_category_discovery():
    """The documented escape hatch, unchanged."""
    targets = run_auctions._search_targets(
        "https://www.ebth.com/browse?q=teak", default_vertical="furniture",
    )
    assert targets[0] == ("furniture", "https://www.ebth.com/browse?q=teak")
    assert not any("category_id=" in u for _v, u in targets)


def test_time_critical_days_is_configurable(monkeypatch):
    monkeypatch.setenv("EBTH_TIME_CRITICAL_DAYS", "5")
    targets = run_auctions._search_targets("", default_vertical="furniture")
    by_vertical = dict(targets)
    assert "days_left=5" in by_vertical[run_auctions._AUTO_ENDING_SOON]


def test_an_explicit_override_keeps_the_clock_sweep_but_drops_the_taste_one():
    """When you name the URLs, you've said what you want searched — so the site-wide
    "recommended" sweep (a discovery *preference*) is dropped rather than flooding your
    selection. The ending-soon sweep stays: time-criticality is the tracker's whole
    purpose, not a preference, and losing it would mean a lot closing in an hour goes
    unseen because it fell outside your query."""
    targets = run_auctions._search_targets(
        "https://www.ebth.com/browse?q=teak\nhttps://www.ebth.com/browse?q=oak",
        default_vertical="furniture",
    )
    assert targets[:2] == [
        ("furniture", "https://www.ebth.com/browse?q=teak"),
        ("furniture", "https://www.ebth.com/browse?q=oak"),
    ]
    assert {v for v, _ in targets[2:]} == {run_auctions._AUTO_ENDING_SOON}


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


def test_an_ending_soon_lot_with_no_quality_signal_is_not_watched():
    """This asserted the opposite until the board proved it wrong. The site-wide
    ending-soon feed bypassed the quality gate on the theory that urgency is itself a
    reason to look — and it filled the entire watchlist with mass-market Barbie lots and
    boxed Christmas ornaments while 584 discovered jewelry lots got no slot at all.

    Urgency tells you *when* a lot must be decided, never *whether* it is worth owning.
    A lot with no signal in any vertical has no honest valuation, so it is not watched."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    cat.lots["x-1"] = AuctionEntry(
        id="x-1", title="Miscellaneous Household Lot", description="assorted items",
        vertical=run_auctions._AUTO_ENDING_SOON, state="ending",
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=5),
    )

    promoted = run_auctions._refresh_watchlist(cat, "furniture", cap=10)

    assert promoted == 0
    assert not cat.lots["x-1"].watch


def test_an_ending_soon_lot_with_a_real_signal_is_watched_and_classified():
    """The feed still earns its keep — it surfaces good lots filed in categories we
    don't trawl. It just has to clear the same bar as everything else."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    cat.lots["x-1"] = AuctionEntry(
        id="x-1", title="Danish Teak Sideboard", description="solid teak, rosewood trim",
        vertical=run_auctions._AUTO_ENDING_SOON, state="ending",
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=5),
    )

    promoted = run_auctions._refresh_watchlist(cat, "furniture", cap=10)

    assert promoted == 1
    assert cat.lots["x-1"].watch
    # Classified for pricing, not left as the raw sentinel.
    assert cat.lots["x-1"].vertical == "furniture"


def test_an_unclassifiable_lot_is_never_given_a_least_bad_vertical():
    """_best_vertical used to fall back to "furniture" when nothing scored, which is how
    a lot of Barbie dolls came to be filed as furniture and valued against furniture
    guidance. A wrong vertical produces a confidently wrong number."""
    from dealfinder.auctions.catalog import AuctionEntry

    now = datetime.now(timezone.utc)
    entry = AuctionEntry(
        id="b-1", title='Mattel #5 Ponytail in "Easter Parade" with Other Barbies',
        description="Lot of 5 vintage Mattel Ponytail Barbie dolls with assorted outfits",
        vertical=run_auctions._AUTO_ENDING_SOON, state="ending",
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=1),
    )
    assert run_auctions._best_vertical(entry) == ""


def test_one_vertical_cannot_take_every_watchlist_slot():
    """Observed live: 51 of 51 watched lots were furniture while 584 jewelry, 154 art and
    111 collectibles lots sat discovered and unwatched. Furniture's keyword list is the
    oldest and richest, so its lots simply outscore jewelry's — ranking by raw score
    across all verticals hands it the whole board on volume alone."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    for i in range(10):
        cat.lots[f"f-{i}"] = AuctionEntry(
            id=f"f-{i}", title="Danish Teak Walnut Sideboard by Lane",
            description="solid teak rosewood mahogany", vertical="furniture", state="live",
            first_seen=now, last_seen=now, ends_at=now + timedelta(hours=6),
        )
    for i in range(10):
        cat.lots[f"j-{i}"] = AuctionEntry(
            id=f"j-{i}", title="14K Gold Diamond Ring",
            description="14k yellow gold", vertical="jewelry", state="live",
            first_seen=now, last_seen=now, ends_at=now + timedelta(hours=6),
        )

    run_auctions._refresh_watchlist(cat, "furniture", cap=10, decide_days=2.0, now=now)

    watched = [e for e in cat.lots.values() if e.watch]
    assert len(watched) == 10
    by_v = collections.Counter(e.vertical for e in watched)
    assert by_v["jewelry"] == 5 and by_v["furniture"] == 5, by_v


def test_an_unused_share_is_not_wasted_on_an_empty_vertical():
    """Fair share is a rotation, not a quota: a vertical with nothing to offer drops out
    and its slots go to the others rather than sitting empty."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    for i in range(6):
        cat.lots[f"f-{i}"] = AuctionEntry(
            id=f"f-{i}", title="Danish Teak Walnut Sideboard by Lane",
            description="solid teak rosewood", vertical="furniture", state="live",
            first_seen=now, last_seen=now, ends_at=now + timedelta(hours=6),
        )
    cat.lots["j-0"] = AuctionEntry(
        id="j-0", title="14K Gold Diamond Ring", description="14k yellow gold",
        vertical="jewelry", state="live",
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=6),
    )

    promoted = run_auctions._refresh_watchlist(cat, "furniture", cap=7, decide_days=2.0, now=now)

    assert promoted == 7, "the lone jewelry lot must not reserve slots furniture could use"


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
    """Full pipeline, dry-run (no AI spend): every priceable EBTH category is actually
    trawled, and a lot from Jewelry and Watches is tagged and watched under jewelry's
    own rules — not silently lost to a furniture keyword gate."""
    from dealfinder.sources.ebth import AuctionItem

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")
    monkeypatch.delenv("EBTH_SEARCH_URLS", raising=False)
    seen_queries: list[str] = []

    def fake_search(self, url, **kw):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        cat = qs.get("category_id", [""])[0]
        seen_queries.append(cat)
        if cat == "3313":          # EBTH's own "Jewelry and Watches"
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
    from dealfinder.auctions.categories import CATEGORIES
    on = [c for c in CATEGORIES if c.default_on]
    # Every priceable category under each of EBTH's three sort presets, plus the two
    # site-wide sweeps.
    assert len(seen_queries) == len(on) * 3 + 2

    cat = load_auction_catalog(tmp_path / "site" / "catalog.json")
    entry = cat.lots["j-1"]
    assert entry.vertical == "jewelry", "the category decides how the lot is priced"
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


def test_a_watchlist_slot_goes_to_a_lot_the_window_can_act_on():
    """Observed live: 31 of 51 watched lots were parked outside the decision window,
    holding slots the appraisal queue could never draw from. A lot closing next week
    scores no better *for the purpose of spending a slot* than one closing tonight — it
    scores worse, because valuation only ever happens inside the window."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    # Strong keyword signal, but closes in nine days — nothing can be decided about it.
    cat.lots["far"] = AuctionEntry(
        id="far", title="Danish Teak Walnut Credenza by Lane", description="rosewood",
        vertical="furniture", state="live",
        first_seen=now, last_seen=now, ends_at=now + timedelta(days=9),
    )
    # Weaker signal, but closes tonight — the only one a valuation can still act on.
    cat.lots["near"] = AuctionEntry(
        id="near", title="Teak and Walnut Side Table", description="",
        vertical="furniture", state="live",
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=6),
    )

    run_auctions._refresh_watchlist(cat, "furniture", cap=1, decide_days=2.0, now=now)

    assert cat.lots["near"].watch, "the actionable lot must take the only slot"
    assert not cat.lots["far"].watch, "a lot outside the window must not hold a slot"


def test_lots_outside_the_window_still_get_watched_when_there_is_room():
    """Window-first is a tie-break for scarce slots, not an exclusion: with room to
    spare, a strong lot closing later is still worth tracking so its bid history is
    already built up by the time it enters the window."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    cat.lots["far"] = AuctionEntry(
        id="far", title="Danish Teak Walnut Credenza by Lane", description="rosewood",
        vertical="furniture", state="live",
        first_seen=now, last_seen=now, ends_at=now + timedelta(days=9),
    )

    run_auctions._refresh_watchlist(cat, "furniture", cap=10, decide_days=2.0, now=now)

    assert cat.lots["far"].watch


def _appraisal(asis=60000, conf=0.8):
    return AppraisalResult(
        identified_item="walnut credenza", maker_guess="Lane",
        est_asis_value_cents=asis, est_restored_resale_value_cents=asis,
        est_restoration_cost_cents=0, est_restoration_effort_hours=0.0,
        confidence=conf, deal_score=60.0,
    )


def test_tightening_the_gate_evicts_lots_it_would_no_longer_admit():
    """The watchlist was write-once, so lots promoted under the old everything-passes
    gate kept their slots for as long as they ran — tightening the gate would have
    changed nothing visible on the board for days."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    cat.lots["junk"] = AuctionEntry(
        id="junk", title='Mattel #5 Ponytail in "Easter Parade" with Other Barbies',
        description="Lot of 5 vintage Mattel Ponytail Barbie dolls",
        vertical="furniture", state="ending", watch=True,
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=1),
    )

    run_auctions._refresh_watchlist(cat, "furniture", cap=150, decide_days=2.0, now=now)

    assert not cat.lots["junk"].watch


def test_an_appraised_lot_the_board_calls_pass_is_not_rescued_from_eviction(monkeypatch):
    """A first pass used a cheap value-beats-bid proxy to protect appraised lots, and it
    kept lots the board was simultaneously labelling PASS — the proxy ignored the fees,
    logistics and margin cushion that actually produce the stance."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    entry = AuctionEntry(
        id="junk", title="Miscellaneous Household Lot", description="assorted",
        vertical="furniture", state="ending", watch=True, current_bid_cents=19500,
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=1),
    )
    entry.appraisal = _appraisal(asis=30000)
    cat.lots["junk"] = entry

    monkeypatch.setattr(
        run_auctions.bidding, "guide",
        lambda e, **kw: type("G", (), {"stance": "outpriced"})(),
    )
    run_auctions._refresh_watchlist(cat, "furniture", cap=150, decide_days=2.0, now=now)

    assert not entry.watch


def test_an_appraised_lot_the_board_calls_bid_survives_a_keyword_it_cannot_satisfy(monkeypatch):
    """The mistitled sleeper this pipeline exists to catch: an appraisal is real evidence
    about a lot and outranks a heuristic about its wording."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    entry = AuctionEntry(
        id="sleeper", title="Miscellaneous Household Lot", description="assorted",
        vertical="furniture", state="ending", watch=True, current_bid_cents=5000,
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=1),
    )
    entry.appraisal = _appraisal(asis=90000)
    cat.lots["sleeper"] = entry

    monkeypatch.setattr(
        run_auctions.bidding, "guide",
        lambda e, **kw: type("G", (), {"stance": "bid"})(),
    )
    run_auctions._refresh_watchlist(cat, "furniture", cap=150, decide_days=2.0, now=now)

    assert entry.watch


def test_a_saturated_watchlist_still_admits_a_newly_eligible_category():
    """Sharing only the *free* room is not sharing. The list saturates at the cap, room
    goes to zero, and whichever vertical qualified first keeps the whole board forever —
    observed live as jewelry holding 109 of 150 slots while 89 qualifying art lots, 28 of
    them inside the decision window, had nowhere to go."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    for i in range(10):  # incumbents, already holding every slot
        cat.lots[f"j-{i}"] = AuctionEntry(
            id=f"j-{i}", title="14K Gold Diamond Ring", description="14k yellow gold",
            vertical="jewelry", state="live", watch=True,
            first_seen=now, last_seen=now, ends_at=now + timedelta(hours=6),
        )
    for i in range(10):  # newly eligible, none watched
        cat.lots[f"a-{i}"] = AuctionEntry(
            id=f"a-{i}", title="Bernard Lennon Oil Portrait Painting", description="",
            vertical="art", state="live",
            first_seen=now, last_seen=now, ends_at=now + timedelta(hours=6),
        )

    run_auctions._refresh_watchlist(cat, "furniture", cap=10, decide_days=2.0, now=now)

    watched = [e for e in cat.lots.values() if e.watch]
    by_v = collections.Counter(e.vertical for e in watched)
    assert len(watched) == 10
    assert by_v["art"] == 5 and by_v["jewelry"] == 5, by_v


def test_losing_a_slot_never_discards_the_appraisal_that_was_paid_for():
    """The one-valuation-per-lot invariant has to survive the allocation churn: a lot
    dropped for a slot and later re-promoted must come back already valued, or the
    rotation would quietly re-buy appraisals every run."""
    from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

    now = datetime.now(timezone.utc)
    cat = AuctionCatalog()
    loser = AuctionEntry(
        id="loser", title="14K Gold Diamond Ring", description="14k yellow gold",
        vertical="jewelry", state="live", watch=True, current_bid_cents=89000,
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=20),
    )
    loser.appraisal = _appraisal(asis=90000)
    cat.lots["loser"] = loser
    # A stronger, sooner-closing rival for the single slot.
    cat.lots["winner"] = AuctionEntry(
        id="winner", title="Tiffany & Co. Sterling Diamond 14K Gold Ring",
        description="sterling 14k platinum diamond", vertical="jewelry", state="live",
        first_seen=now, last_seen=now, ends_at=now + timedelta(hours=1),
    )

    run_auctions._refresh_watchlist(cat, "furniture", cap=1, decide_days=2.0, now=now)
    assert not loser.watch, "the fixture must actually evict the appraised lot"

    # Room opens up again; the lot returns already valued.
    run_auctions._refresh_watchlist(cat, "furniture", cap=10, decide_days=2.0, now=now)

    assert loser.watch
    assert loser.appraisal is not None
    assert loser.appraisal.est_asis_value_cents == 90000
