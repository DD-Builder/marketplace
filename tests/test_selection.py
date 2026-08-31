"""Cost-control funnel tests: dedup, seen-diff, and the appraisal cap."""

from __future__ import annotations

from dealfinder.core.schemas import RawListing, RawPhoto
from dealfinder.selection import (
    dedup_listings,
    diff_new_and_changed,
    plan_appraisals,
    select_for_appraisal,
    update_seen,
)


def _l(id_, title="", desc="", price=5000, photos=1):
    return RawListing(
        fb_listing_id=id_,
        title=title,
        description=desc,
        asking_price_cents=price,
        photos=[RawPhoto(remote_url=f"u{i}", position=i) for i in range(photos)],
    )


def test_dedup_collapses_cross_search_overlap():
    # Same id surfaced by 'dresser', 'mcm', and 'walnut' searches -> one record.
    listings = [_l("A"), _l("B"), _l("A"), _l("A")]
    out = dedup_listings(listings)
    assert {x.fb_listing_id for x in out} == {"A", "B"}


def test_dedup_keeps_richest_record():
    thin = _l("A", price=None, photos=1)
    rich = _l("A", price=8000, photos=6)
    out = dedup_listings([thin, rich])
    assert len(out) == 1
    assert out[0].asking_price_cents == 8000 and len(out[0].photos) == 6


def test_diff_flags_new_and_price_drops_only():
    seen = {"A": 10000, "B": 5000}
    listings = [
        _l("A", price=8000),   # price dropped -> actionable
        _l("B", price=5000),   # unchanged -> skip (don't pay)
        _l("C", price=3000),   # new -> actionable
    ]
    diff = diff_new_and_changed(listings, seen)
    ids = lambda xs: {x.fb_listing_id for x in xs}
    assert ids(diff.new) == {"C"}
    assert ids(diff.price_dropped) == {"A"}
    assert ids(diff.unchanged) == {"B"}
    assert ids(diff.actionable) == {"A", "C"}


def test_price_increase_is_not_actionable():
    seen = {"A": 5000}
    diff = diff_new_and_changed([_l("A", price=9000)], seen)
    assert not diff.actionable


def test_update_seen_records_prices():
    seen = update_seen({}, [_l("A", price=5000), _l("B", price=None)])
    assert seen == {"A": 5000, "B": None}


def test_select_caps_strong_and_adds_wildcards():
    strong = [_l(f"s{i}", title="solid walnut mid-century dresser", price=6000) for i in range(30)]
    wild = [_l(f"w{i}", title="old dresser", desc="needs work", price=4000) for i in range(10)]
    sel = select_for_appraisal(strong + wild, top_n=20, wildcards=5)
    assert len(sel.strong) == 20
    assert len(sel.wildcards) == 5
    assert len(sel.to_appraise) == 25
    assert sel.over_cap == 10  # 30 strong - 20 cap


def test_plan_reports_full_funnel_counts():
    seen = {"old": 5000}
    listings = (
        [_l("old", title="teak sideboard", price=5000)]                 # seen, unchanged
        + [_l(f"n{i}", title="solid oak dresser", price=6000) for i in range(3)]  # new strong
        + [_l("junk", title="IKEA Malm", desc="particle board")]        # junked by prescreen
    )
    plan = plan_appraisals(listings, seen, top_n=20, wildcards=5)
    assert plan.total_scraped == 5
    assert plan.skipped_seen == 1          # the 'old' listing, not re-appraised
    assert plan.new == 4                   # 3 oak + junk are both "new" ids
    assert plan.dropped_by_prescreen == 1  # the IKEA one
    assert len(plan.to_appraise) == 3      # only the real oak dressers
    assert "already-seen" in plan.summary()
