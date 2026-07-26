"""Two-stage scrape: prove we pay for the grid, and then only for what's worth reading.

Every test drives an injected fetcher that records the exact actor inputs, so the assertions
are about *what would have been billed* — which is the whole point of the module.
"""

from __future__ import annotations

import urllib.parse

import pytest

from dealfinder.core.schemas import RawListing, RawPhoto
from dealfinder.sources.scrape import (
    DetailStageUnsupported,
    SearchFilters,
    apply_filters,
    item_url,
    scrape,
    select_for_detail,
)

SEARCH = "https://www.facebook.com/marketplace/lexington/search/?query=dresser"


def _l(id_, price=4000, photos=1, detail=False, desc=""):
    return RawListing(
        fb_listing_id=id_,
        title=f"solid oak dresser {id_}",
        description=desc,
        asking_price_cents=price,
        url=f"https://www.facebook.com/marketplace/item/{id_}/",
        photos=[RawPhoto(remote_url=f"u{i}", position=i) for i in range(photos)],
        detail_fetched=detail,
    )


class FakeActor:
    """Records every run input, and answers index vs detail requests differently."""

    def __init__(self, grid, *, detail_works=True, detail_returns_thin=False):
        self.grid = {lst.fb_listing_id: lst for lst in grid}
        self.detail_works = detail_works
        self.detail_returns_thin = detail_returns_thin
        self.calls: list[tuple[dict, str]] = []

    def __call__(self, run_input: dict, actor: str) -> list[RawListing]:
        self.calls.append((run_input, actor))
        starts = [s["url"] for s in run_input["startUrls"]]
        wants_detail = run_input.get("includeListingDetails", False)
        if any("/marketplace/item/" in u for u in starts):        # Stage B
            if not self.detail_works:
                raise RuntimeError("actor rejected item URLs")
            if self.detail_returns_thin:
                return []
            ids = [u.rstrip("/").rsplit("/", 1)[-1] for u in starts]
            return [self._detailed(self.grid[i]) for i in ids if i in self.grid]
        rows = list(self.grid.values())[: run_input["resultsLimit"]]
        return [self._detailed(r) for r in rows] if wants_detail else [self._thin(r) for r in rows]

    @staticmethod
    def _thin(lst):
        return lst.model_copy(update={"description": "", "detail_fetched": False})

    @staticmethod
    def _detailed(lst):
        return lst.model_copy(
            update={"description": "solid oak, dovetailed drawers", "detail_fetched": True}
        )

    @property
    def index_calls(self):
        return [c for c, _ in self.calls if not any(
            "/marketplace/item/" in s["url"] for s in c["startUrls"])]

    @property
    def detail_calls(self):
        return [c for c, _ in self.calls if any(
            "/marketplace/item/" in s["url"] for s in c["startUrls"])]

    def detail_ids(self):
        return [
            s["url"].rstrip("/").rsplit("/", 1)[-1]
            for call in self.detail_calls for s in call["startUrls"]
        ]


# --- search-URL filters -------------------------------------------------------------------

def test_filters_are_pushed_into_the_search_url():
    url = apply_filters(SEARCH, SearchFilters(
        min_price_dollars=25, max_price_dollars=800, days_since_listed=1, radius_km=64,
    ))
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q == {
        "query": "dresser", "minPrice": "25", "maxPrice": "800",
        "daysSinceListed": "1", "radius": "64", "sortBy": "creation_time_descend",
    }


def test_a_hand_written_url_always_wins():
    url = apply_filters(SEARCH + "&maxPrice=200", SearchFilters(max_price_dollars=800))
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["maxPrice"] == "200"


def test_empty_filters_leave_the_url_untouched():
    assert apply_filters(SEARCH, None) == SEARCH
    assert apply_filters(SEARCH, SearchFilters(newest_first=False)) == SEARCH


def test_item_url_is_synthesised_when_the_grid_omits_it():
    assert item_url(_l("123")).endswith("/marketplace/item/123/")
    bare = _l("456").model_copy(update={"url": ""})
    assert item_url(bare) == "https://www.facebook.com/marketplace/item/456/"


# --- selection ----------------------------------------------------------------------------

def test_new_and_newly_cheaper_listings_are_read_first():
    index = [_l("new"), _l("drop", price=3000), _l("same", price=5000)]
    seen = {"drop": 9000, "same": 5000}
    picked = [lst.fb_listing_id for lst in select_for_detail(index, seen, cap=2)]
    assert set(picked) == {"new", "drop"}


def test_leftover_budget_rescues_listings_a_previous_cap_stranded():
    """Without this a listing cut by the cap is stranded: next run it is neither new nor
    cheaper, so it would never qualify again and would sit on a title alone forever."""
    index = [_l("new"), _l("stranded", price=5000)]
    seen = {"stranded": 5000}

    tight = [lst.fb_listing_id for lst in select_for_detail(index, seen, cap=1)]
    assert tight == ["new"]                        # today's find still wins the budget

    roomy = [lst.fb_listing_id for lst in select_for_detail(index, seen, cap=5)]
    assert roomy == ["new", "stranded"]            # ...and the leftover picks up the rest


def test_already_detailed_ids_are_never_re_fetched():
    """A price drop on a piece we've already read is free: no scrape, and (via the
    catalogue) no appraisal either."""
    index = [_l("a", price=3000)]
    picked = select_for_detail(index, {"a": 9000}, already_detailed={"a"})
    assert picked == []


def test_detail_cap_keeps_the_richest_records():
    index = [_l("thin", photos=1), _l("rich", photos=5), _l("mid", photos=3)]
    picked = select_for_detail(index, {}, cap=2)
    assert [lst.fb_listing_id for lst in picked] == ["rich", "mid"]


# --- the happy path ------------------------------------------------------------------------

def test_two_stage_requests_exactly_the_expected_ids():
    actor = FakeActor([_l("new"), _l("drop", price=3000), _l("same", price=5000)])
    res = scrape([SEARCH], {"drop": 9000, "same": 5000}, fetch=actor, already_detailed={"same"})

    assert res.mode == "two-stage" and res.detail_supported is True
    assert len(actor.index_calls) == 1
    assert actor.index_calls[0]["includeListingDetails"] is False   # the cheap pass
    assert len(actor.detail_calls) == 1                             # ONE batched detail run
    assert sorted(actor.detail_ids()) == ["drop", "new"]

    by_id = {lst.fb_listing_id: lst for lst in res.listings}
    assert by_id["new"].detail_fetched and by_id["drop"].detail_fetched
    assert not by_id["same"].detail_fetched          # untouched, and unbilled
    assert "dovetailed" in by_id["new"].description


def test_a_run_with_nothing_new_and_nothing_thin_never_opens_a_detail_page():
    actor = FakeActor([_l("a", price=5000), _l("b", price=5000)])
    res = scrape([SEARCH], {"a": 5000, "b": 5000}, fetch=actor, already_detailed={"a", "b"})
    assert actor.detail_calls == []
    assert res.detail_requested == [] and res.index_count == 2


def test_one_failing_search_does_not_sink_the_run():
    actor = FakeActor([_l("a")])
    bad = "https://www.facebook.com/marketplace/lexington/search/?query=boom"

    def fetch(run_input, actor_id):
        if "boom" in run_input["startUrls"][0]["url"]:
            raise RuntimeError("actor blew up")
        return actor(run_input, actor_id)

    res = scrape([bad, SEARCH], {}, fetch=fetch)
    assert len(res.searches_failed) == 1 and res.index_count == 1


# --- coverage -------------------------------------------------------------------------------

def test_coverage_reports_truncation_so_absence_is_not_read_as_death():
    actor = FakeActor([_l(str(i)) for i in range(5)])
    full = scrape([SEARCH], {}, fetch=actor, results_limit=10)
    assert full.coverage[SEARCH].truncated is False

    capped = scrape(
        [SEARCH], {}, fetch=FakeActor([_l(str(i)) for i in range(5)]), results_limit=5
    )
    assert capped.coverage[SEARCH].truncated is True


# --- the fallback ladder ---------------------------------------------------------------------

def test_rung_two_falls_back_to_a_single_detailed_pass():
    actor = FakeActor([_l("a"), _l("b")], detail_works=False)
    res = scrape([SEARCH], {}, fetch=actor)

    assert res.mode == "single-stage"
    assert res.detail_supported is False              # remembered, so it costs once
    assert res.notes and "unsupported" in res.notes[0]
    assert all(lst.detail_fetched for lst in res.listings)
    assert len(actor.index_calls) == 2                # cheap pass, then the detailed retry


def test_a_remembered_failure_skips_the_wasted_probe():
    actor = FakeActor([_l("a")], detail_works=False)
    res = scrape([SEARCH], {}, fetch=actor, detail_supported=False)
    assert res.mode == "single-stage"
    assert len(actor.calls) == 1                      # no second bill for the same rows
    assert actor.calls[0][0]["includeListingDetails"] is True


def test_rung_three_keeps_thin_records_queued_for_enrichment():
    class NoDetailAnywhere(FakeActor):
        def __call__(self, run_input, actor):
            self.calls.append((run_input, actor))
            starts = [s["url"] for s in run_input["startUrls"]]
            if any("/marketplace/item/" in u for u in starts):
                raise RuntimeError("actor rejected item URLs")
            return [self._thin(r) for r in self.grid.values()]   # details never arrive

    actor = NoDetailAnywhere([_l("a"), _l("b")])
    res = scrape([SEARCH], {}, fetch=actor)

    assert res.mode == "thin-only"
    assert res.detail_supported is False
    assert res.listings and not any(lst.detail_fetched for lst in res.listings)
    assert any("thin" in n for n in res.notes)


def test_an_empty_detail_run_counts_as_unsupported():
    actor = FakeActor([_l("a")], detail_returns_thin=True)
    res = scrape([SEARCH], {}, fetch=actor)
    assert res.detail_supported is False and res.mode == "single-stage"


def test_detail_actor_override_is_used_for_stage_b_only():
    actor = FakeActor([_l("a")])
    scrape([SEARCH], {}, fetch=actor, actor="index~actor", detail_actor="detail~actor")
    used = {a for _, a in actor.calls}
    assert used == {"index~actor", "detail~actor"}
    assert [a for c, a in actor.calls if "/marketplace/item/" in c["startUrls"][0]["url"]] \
        == ["detail~actor"]


def test_fetch_details_raises_rather_than_returning_junk():
    from dealfinder.sources.scrape import _fetch_details

    def boom(run_input, actor):
        raise RuntimeError("nope")

    with pytest.raises(DetailStageUnsupported):
        _fetch_details([_l("a")], fetch=boom, actor="x", timeout_limit=1)


# --- the economics -----------------------------------------------------------------------------

def test_simulated_daily_cycle_bills_detail_for_only_the_new_listings():
    """The measured shape of a daily re-run: ~30% new, ~70% already seen. The point of the
    whole module is that only the 30% reaches a detail page."""
    grid = [_l(f"day0-{i}") for i in range(20)]
    actor = FakeActor(grid)
    seen: dict[str, int | None] = {}

    first = scrape([SEARCH], seen, fetch=actor, results_limit=50)
    assert first.detail_ratio == 1.0                       # cold start reads everything
    seen = {lst.fb_listing_id: lst.asking_price_cents for lst in first.listings}
    detailed = {lst.fb_listing_id for lst in first.listings if lst.detail_fetched}

    # Next day: 6 of 20 are new (30%), the rest unchanged.
    grid = grid[6:] + [_l(f"day1-{i}") for i in range(6)]
    actor = FakeActor(grid)
    second = scrape([SEARCH], seen, fetch=actor, already_detailed=detailed, results_limit=50)

    assert len(second.detail_requested) == 6
    assert second.detail_ratio == pytest.approx(0.30, abs=0.01)
    assert all(i.startswith("day1-") for i in second.detail_requested)
    assert len(actor.detail_calls) == 1                     # still one batched run
