"""The catalogue is what makes runs accumulate — these tests pin that behaviour.

The three things that must hold, because breaking any one of them silently costs money or
silently empties the board:

1. a stored appraisal survives a round-trip through JSON and re-scores identically;
2. an unchanged listing is never re-appraised;
3. a listing missing from one truncated scan is *not* declared gone.
"""

from __future__ import annotations

from datetime import timedelta

from dealfinder import catalog as cat
from dealfinder.core.schemas import AppraisalResult, RawListing, RawPhoto
from dealfinder.engine import evaluate_piece, run_valuation
from dealfinder.selection import diff_new_and_changed

from tests.test_engine import StubProvider


def _l(id_, title="solid oak dresser", price=4000, photos=1, desc="", detail=False, sold=None):
    return RawListing(
        fb_listing_id=id_, title=title, asking_price_cents=price, description=desc,
        url=f"https://fb.com/{id_}", location_text="Lexington, KY",
        photos=[RawPhoto(remote_url=f"u{i}", position=i) for i in range(photos)],
        detail_fetched=detail, is_sold=sold,
    )


def _cov(url="s1", truncated=False, count=10):
    return {url: cat.SearchCoverage(url=url, last_count=count, truncated=truncated)}


# --- observe ---------------------------------------------------------------------------

def test_observe_records_new_listings_and_price_history():
    c = cat.Catalog()
    rep = cat.observe(c, [_l("a", price=5000)])
    assert rep.new == 1 and c.listings["a"].asking_price_cents == 5000

    rep = cat.observe(c, [_l("a", price=3000)])
    assert rep.new == 0 and rep.price_drops == 1
    entry = c.listings["a"]
    assert entry.asking_price_cents == 3000
    assert [p.cents for p in entry.price_history] == [5000, 3000]


def test_observe_does_not_mark_gone_on_a_truncated_scan():
    """resultsLimit routinely hides live pieces. Treating absence as death would delete
    the board every time a search filled its quota."""
    c = cat.Catalog()
    cat.observe(c, [_l("a"), _l("b")])
    for _ in range(5):
        cat.observe(c, [_l("a")], coverage=_cov(truncated=True))
    assert c.listings["b"].state == "live"
    assert c.listings["b"].misses == 5


def test_observe_marks_gone_after_two_misses_on_full_coverage():
    c = cat.Catalog()
    cat.observe(c, [_l("a"), _l("b")])
    cat.observe(c, [_l("a")], coverage=_cov(truncated=False))
    assert c.listings["b"].state == "live"          # one miss is not evidence
    rep = cat.observe(c, [_l("a")], coverage=_cov(truncated=False))
    assert c.listings["b"].state == "gone" and rep.marked_gone == 1

    back = cat.observe(c, [_l("a"), _l("b")])
    assert c.listings["b"].state == "live" and back.returned_to_live == 1


def test_observe_falls_back_to_age_when_coverage_is_always_truncated():
    c = cat.Catalog()
    cat.observe(c, [_l("a"), _l("b")])
    c.listings["b"].last_seen = c.listings["b"].last_seen - timedelta(days=20)
    cat.observe(c, [_l("a")], coverage=_cov(truncated=True))
    assert c.listings["b"].state == "gone"


def test_observe_marks_sold():
    c = cat.Catalog()
    cat.observe(c, [_l("a", price=5000)])
    rep = cat.observe(c, [_l("a", price=5000, sold=True)])
    assert rep.marked_sold == 1
    assert c.listings["a"].state == "sold" and c.listings["a"].sold_price_cents == 5000


def test_observe_upgrades_a_thin_record_when_details_arrive():
    c = cat.Catalog()
    cat.observe(c, [_l("a", desc="", detail=False)])
    assert not c.listings["a"].detail_fetched
    cat.observe(c, [_l("a", desc="solid walnut, dovetailed", detail=True)])
    assert c.listings["a"].detail_fetched
    assert "dovetailed" in c.listings["a"].description


# --- the seen-ledger hinge --------------------------------------------------------------

def test_seen_view_drives_the_existing_diff_unchanged():
    c = cat.Catalog()
    cat.observe(c, [_l("a", price=5000), _l("b", price=2000)])
    diff = diff_new_and_changed(
        [_l("a", price=5000), _l("b", price=1000), _l("c", price=900)],
        cat.seen_view(c),
    )
    assert [x.fb_listing_id for x in diff.new] == ["c"]
    assert [x.fb_listing_id for x in diff.price_dropped] == ["b"]
    assert [x.fb_listing_id for x in diff.unchanged] == ["a"]


# --- appraisal storage ------------------------------------------------------------------

def test_stored_appraisal_round_trips_and_rescores_identically(tmp_path):
    """The heart of it: value once, re-rank for free forever."""
    listing = _l("p1", title="Lane walnut credenza", price=6000)
    c = cat.Catalog()
    cat.observe(c, [listing])
    res = run_valuation([listing], seen={}, provider=StubProvider(), hourly_rate_cents=3000)
    cat.record_appraisals(c, res.pieces, appraiser="stub")

    path = tmp_path / "catalog.json"
    cat.save_catalog(c, path)
    reloaded = cat.load_catalog(path)

    entry = reloaded.listings["p1"]
    assert entry.appraiser == "stub" and entry.appraisal is not None
    rescored = evaluate_piece(entry.to_listing(), entry.appraisal, hourly_rate_cents=3000)
    live = res.pieces[0]
    for f in ("deal_score", "cash_margin_cents", "liquidity", "heat", "priority", "is_killer"):
        assert getattr(rescored, f) == getattr(live, f), f


def test_a_price_drop_on_a_catalogued_piece_costs_nothing(tmp_path):
    listing = _l("p2", title="solid oak dresser", price=8000)
    c = cat.Catalog()
    cat.observe(c, [listing])
    res = run_valuation([listing], seen={}, provider=StubProvider())
    cat.record_appraisals(c, res.pieces, appraiser="stub")
    before = evaluate_piece(c.listings["p2"].to_listing(), c.listings["p2"].appraisal)

    cat.observe(c, [_l("p2", title="solid oak dresser", price=3000)])
    entry = c.listings["p2"]
    after = evaluate_piece(entry.to_listing(), entry.appraisal)

    assert entry.appraisal is not None          # no re-appraisal happened
    assert after.cash_margin_cents > before.cash_margin_cents
    assert after.priority >= before.priority


def test_a_price_drop_on_a_valued_piece_buys_no_new_ai_call():
    """AUDIT 2 caught this: the seen-diff rightly calls a price drop actionable, but an
    appraisal describes the *object*, which a discount does not change."""
    from dealfinder.selection import plan_appraisals

    c = cat.Catalog()
    cat.observe(c, [_l("a", price=8000), _l("b", price=8000)])
    res = run_valuation([_l("a", price=8000)], seen={}, provider=StubProvider())
    cat.record_appraisals(c, res.pieces)

    cheaper = [_l("a", price=3000), _l("b", price=3000)]
    obs = cat.observe(c, cheaper)
    assert obs.price_drops == 2

    plan = plan_appraisals(
        cheaper, {"a": 8000, "b": 8000},
        already_valued=cat.already_valued(c, exclude=obs.detail_upgrades),
    )
    assert [x.fb_listing_id for x in plan.to_appraise] == ["b"]   # 'a' is free to re-rank
    assert plan.price_dropped == 2 and plan.skipped_already_valued == 1
    assert "1 re-ranked free" in plan.summary()


def test_a_thin_record_that_gains_detail_is_worth_re_valuing():
    from dealfinder.selection import plan_appraisals

    c = cat.Catalog()
    cat.observe(c, [_l("a", price=8000, desc="", detail=False)])
    res = run_valuation([_l("a", price=8000)], seen={}, provider=StubProvider())
    cat.record_appraisals(c, res.pieces)

    # Same price as before — deliberately. The upgrade alone must be enough: an earlier
    # version of this test lowered the price too, which routed 'a' through the ordinary
    # price-drop path and hid that the upgrade path didn't work at all.
    full = [_l("a", price=8000, desc="solid walnut, dovetail drawers", detail=True)]
    obs = cat.observe(c, full)
    assert obs.detail_upgrades == ["a"]

    # Production wiring: an unchanged-price listing is invisible to the seen-diff, so the
    # redo candidates must arrive through the backfill pool, not just be un-skipped.
    redo = [c.listings[i].to_listing() for i in obs.detail_upgrades]
    plan = plan_appraisals(
        full, {"a": 8000},
        backfill=redo + cat.unappraised_live(c),
        already_valued=cat.already_valued(c, exclude=obs.detail_upgrades),
    )
    assert [x.fb_listing_id for x in plan.to_appraise] == ["a"]
    # ...and only once: the upgrade isn't reported again on the next scan.
    assert cat.observe(c, full).detail_upgrades == []


def test_backfill_never_reoffers_an_already_valued_piece():
    from dealfinder.selection import plan_appraisals

    c = cat.Catalog()
    cat.observe(c, [_l("a"), _l("b")])
    res = run_valuation([_l("a")], seen={}, provider=StubProvider())
    cat.record_appraisals(c, res.pieces)

    plan = plan_appraisals(
        [_l("a"), _l("b")], cat.seen_view(c),
        backfill=[e.to_listing() for e in c.listings.values()],
        already_valued=cat.already_valued(c),
    )
    assert [x.fb_listing_id for x in plan.to_appraise] == ["b"]
    assert plan.backfilled == 1


def test_needs_reappraisal_only_on_genuinely_new_evidence():
    entry = cat.CatalogEntry(
        id="a", first_seen=cat._now(), last_seen=cat._now(), detail_fetched=False,
        appraisal=AppraisalResult(
            identified_item="dresser", est_asis_value_cents=1,
            est_restored_resale_value_cents=2, est_restoration_cost_cents=0,
            est_restoration_effort_hours=0.0, deal_score=1.0, confidence=0.5,
        ),
    )
    assert cat.needs_reappraisal(entry, _l("a", detail=True))
    assert not cat.needs_reappraisal(entry, _l("a", detail=False))
    entry.detail_fetched = True
    assert not cat.needs_reappraisal(entry, _l("a", detail=True))
    unvalued = entry.model_copy(update={"appraisal": None})
    assert not cat.needs_reappraisal(unvalued, _l("a", detail=True))


# --- views, backfill, persistence -------------------------------------------------------

def test_live_entries_and_backfill_pool_are_complements():
    c = cat.Catalog()
    cat.observe(c, [_l("a"), _l("b"), _l("c")])
    res = run_valuation([_l("a")], seen={}, provider=StubProvider())
    cat.record_appraisals(c, res.pieces)
    c.listings["c"].state = "gone"

    assert [e.id for e in cat.live_entries(c)] == ["a"]
    assert {x.fb_listing_id for x in cat.unappraised_live(c)} == {"b"}


def test_migrate_from_seen_preserves_prices_so_nothing_is_re_appraised():
    c = cat.migrate_from_seen({"a": 5000, "b": None})
    assert cat.seen_view(c) == {"a": 5000, "b": None}
    diff = diff_new_and_changed([_l("a", price=5000)], cat.seen_view(c))
    assert not diff.actionable


def test_a_corrupt_catalog_is_quarantined_never_silently_replaced(tmp_path):
    """The old behaviour returned an empty catalogue, which the run then SAVED over the
    damaged file — one truncated write destroyed every stored appraisal. Now: back up,
    keep the original byte-for-byte, and refuse to run."""
    import pytest

    path = tmp_path / "catalog.json"
    path.write_text("{ this is not json")
    with pytest.raises(cat.CatalogCorrupt):
        cat.load_catalog(path)
    backups = list(tmp_path.glob("catalog.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "{ this is not json"
    assert path.read_text() == "{ this is not json"   # original untouched

    # A catalogue from a NEWER schema version is quarantined too — loading it through
    # the old model could drop fields and then persist the loss.
    newer = tmp_path / "newer.json"
    newer.write_text('{"version": 999, "listings": {}}')
    with pytest.raises(cat.CatalogCorrupt):
        cat.load_catalog(newer)

    # A missing file is a fresh start, not an error.
    assert cat.load_catalog(tmp_path / "missing.json").listings == {}


def test_save_catalog_is_deterministic(tmp_path):
    c = cat.Catalog()
    cat.observe(c, [_l("b"), _l("a")])
    p1, p2 = tmp_path / "1.json", tmp_path / "2.json"
    cat.save_catalog(c, p1)
    cat.save_catalog(cat.load_catalog(p1), p2)
    # Only updated_at may differ; the listing block must be byte-identical and sorted.
    import json
    assert json.loads(p1.read_text())["listings"] == json.loads(p2.read_text())["listings"]
    assert list(json.loads(p1.read_text())["listings"]) == ["a", "b"]


def test_prune_bounds_the_file_but_never_drops_a_live_piece():
    c = cat.Catalog()
    cat.observe(c, [_l("live"), _l("old_gone"), _l("old_sold"), _l("fresh_gone")])
    now = cat._now()
    for cid, state, days in (
        ("old_gone", "gone", 100), ("old_sold", "sold", 200), ("fresh_gone", "gone", 3),
    ):
        c.listings[cid].state = state
        c.listings[cid].last_seen = now - timedelta(days=days)
    c.listings["live"].last_seen = now - timedelta(days=400)

    rep = cat.prune(c)
    assert set(rep.removed_ids) == {"old_gone", "old_sold"}
    assert set(c.listings) == {"live", "fresh_gone"}


def test_prune_drops_unappraised_dead_entries_sooner():
    c = cat.Catalog()
    cat.observe(c, [_l("a")])
    c.listings["a"].state = "gone"
    c.listings["a"].last_seen = cat._now() - timedelta(days=40)
    assert cat.prune(c).removed_ids == ["a"]


# --- blind valuations ---------------------------------------------------------------------

def test_a_valuation_made_without_photos_is_redone_once_photos_exist():
    """Otherwise the first thin guess is locked in forever: the record is already
    'detailed', so nothing would ever trigger a second look."""
    c = cat.Catalog()
    cat.observe(c, [_l("a", desc="solid oak dresser", detail=True)])
    res = run_valuation([_l("a")], seen={}, provider=StubProvider())
    cat.record_appraisals(c, res.pieces, saw_photos=[])       # the CDN was unreachable

    entry = c.listings["a"]
    assert not entry.appraised_with_photos
    assert cat.blind_appraisals(c) == {"a"}
    assert cat.needs_reappraisal(entry, _l("a", detail=True), has_photos=True)
    assert not cat.needs_reappraisal(entry, _l("a", detail=True), has_photos=False)

    # Re-valued with a photo this time, it stops being offered.
    cat.record_appraisals(c, res.pieces, saw_photos=["a"])
    assert c.listings["a"].appraised_with_photos
    assert cat.blind_appraisals(c) == set()
    assert not cat.needs_reappraisal(c.listings["a"], _l("a", detail=True), has_photos=True)


def test_blind_pieces_are_excluded_from_already_valued_so_they_win_budget():
    from dealfinder.selection import plan_appraisals

    c = cat.Catalog()
    cat.observe(c, [_l("blind"), _l("seen")])
    res = run_valuation([_l("blind"), _l("seen")], seen={}, provider=StubProvider())
    cat.record_appraisals(c, res.pieces, saw_photos=["seen"])

    valued = cat.already_valued(c, exclude=cat.blind_appraisals(c))
    assert valued == {"seen"}
    # Production wiring: the blind piece has an appraisal, so unappraised_live() will
    # never offer it — the redo pool must inject it into backfill explicitly. An earlier
    # version passed *every* entry as backfill, which production never does, and so
    # asserted a behaviour the pipeline didn't actually have.
    redo = [c.listings[i].to_listing() for i in sorted(cat.blind_appraisals(c))]
    plan = plan_appraisals(
        [_l("blind"), _l("seen")], cat.seen_view(c),
        backfill=redo + cat.unappraised_live(c), already_valued=valued,
    )
    assert [x.fb_listing_id for x in plan.to_appraise] == ["blind"]


def test_a_seeded_catalogue_still_blocks_re_scraping_and_re_appraisal():
    """The point of seeding from a scrape you already paid for."""
    from dealfinder.sources.scrape import select_for_detail

    c = cat.Catalog()
    cat.observe(c, [_l("a", price=5000, detail=True), _l("b", price=5000, detail=True)])
    res = run_valuation([_l("a", price=5000)], seen={}, provider=StubProvider())
    cat.record_appraisals(c, res.pieces, saw_photos=["a"])

    # Nothing to fetch again...
    assert select_for_detail(
        [_l("a", price=5000, detail=True), _l("b", price=5000, detail=True)],
        cat.seen_view(c), already_detailed=cat.detailed_ids(c),
    ) == []
    # ...and 'a' is not re-valued, while the never-valued 'b' still is.
    assert cat.already_valued(c, exclude=cat.blind_appraisals(c)) == {"a"}
    assert {x.fb_listing_id for x in cat.unappraised_live(c)} == {"b"}


# --- evidence age ---------------------------------------------------------------------

def test_recovered_data_does_not_masquerade_as_a_fresh_sighting():
    """The board put already-sold pieces on top because folding in a three-day-old
    recovered dataset stamped last_seen = now, making stale evidence look confirmed."""
    from datetime import datetime, timezone

    three_days_ago = cat._now() - timedelta(days=3)
    c = cat.Catalog()
    cat.observe(c, [_l("a").model_copy(update={"observed_at": three_days_ago})])
    entry = c.listings["a"]
    assert entry.last_seen == three_days_ago
    assert entry.first_seen == three_days_ago

    # A live scrape (no observed_at) is "now", and never moves last_seen backwards.
    cat.observe(c, [_l("a")])
    assert c.listings["a"].last_seen > three_days_ago
    cat.observe(c, [_l("a").model_copy(update={"observed_at": three_days_ago})])
    assert c.listings["a"].last_seen > three_days_ago, "an older sighting must not win"

    assert datetime.now(timezone.utc) >= c.listings["a"].last_seen


def test_a_stale_piece_is_flagged_and_pushed_down_the_board():
    from dealfinder.ranking import staleness_factor

    assert staleness_factor(0) == 1.0
    assert staleness_factor(1) == 1.0
    assert staleness_factor(14) == 0.25
    assert staleness_factor(60) == 0.25
    assert 0.25 < staleness_factor(7) < 1.0          # a week old is a coin toss

    listing = _l("s", title="Lane walnut credenza", price=6000)
    appraisal = StubProvider().appraise(listing, None)
    fresh = evaluate_piece(listing, appraisal, days_since_seen=0)
    stale = evaluate_piece(listing, appraisal, days_since_seen=12)

    assert stale.priority < fresh.priority
    assert any("Unconfirmed" in b.label for b in stale.badges)
    assert not any("Unconfirmed" in b.label for b in fresh.badges)
