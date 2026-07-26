"""Two-stage scraping: pay for the search grid, then pay for *only* the pages worth reading.

The single biggest cost mistake in this project was asking the actor for every listing's
detail page on every run. Apify bills at fetch time, so a run that already knew about 70%
of what it found still paid to re-read all of it. Measured on the real 91-listing export,
``description`` and ``listingPhotos`` are the *only* fields that need the detail page —
title, price, location, photo thumbnail, sold/live flags and post date all come free in the
search grid.

So:

* **Stage A** — one cheap index scan per search URL (``includeListingDetails: False``).
* **Stage B** — one detail run for the handful of ids that are new or newly cheaper *and*
  not already detailed. A price drop on a piece we've already read costs nothing at all.

Two things keep this honest:

* **Search-URL filters.** 19% of the measured scrape went to listings outside the radius or
  price range — waste that was billed before we could discard it. Pushing price and recency
  filters into the URL removes it at the source.
* **A fallback ladder.** Fetching item URLs through this actor is unverified, so Stage B is
  allowed to fail: it degrades to a single-stage scan, then to thin-only records queued for
  later enrichment. The verdict is written to the catalogue, so a failed probe is paid for
  once rather than every run.

Everything here takes an injected ``fetch`` callable, so the whole ladder is tested without
a network or a cent of credit.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from dealfinder.catalog import SearchCoverage
from dealfinder.core.schemas import RawListing
from dealfinder.logging import get_logger
from dealfinder.selection import dedup_listings, diff_new_and_changed

log = get_logger(__name__)

DEFAULT_ACTOR = "apify~facebook-marketplace-scraper"

#: ``fetch(run_input, actor) -> listings``. Injected so tests never touch the network.
Fetcher = Callable[[dict, str], list[RawListing]]

Mode = Literal["two-stage", "single-stage", "thin-only"]


class DetailStageUnsupported(RuntimeError):
    """Stage B could not fetch item pages with this actor — fall down the ladder."""


# --- search-URL filters ------------------------------------------------------------------

@dataclass(frozen=True)
class SearchFilters:
    """Filters pushed into the Marketplace search URL so waste is never billed.

    These are Facebook's own search parameters, taken from the query string its UI produces.
    They are passed through verbatim: if Facebook renames one, the filter is simply ignored
    by Facebook and we scrape as before — a lost saving, not a broken run. Parameters
    already present in a hand-written URL always win, so your own URL is never overridden.
    """

    min_price_dollars: int | None = None
    max_price_dollars: int | None = None
    days_since_listed: int | None = None
    radius_km: int | None = None
    newest_first: bool = True

    def params(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.min_price_dollars is not None:
            out["minPrice"] = str(self.min_price_dollars)
        if self.max_price_dollars is not None:
            out["maxPrice"] = str(self.max_price_dollars)
        if self.days_since_listed is not None:
            out["daysSinceListed"] = str(self.days_since_listed)
        if self.radius_km is not None:
            out["radius"] = str(self.radius_km)
        if self.newest_first:
            out["sortBy"] = "creation_time_descend"
        return out


def apply_filters(url: str, filters: SearchFilters | None) -> str:
    """Merge ``filters`` into a Marketplace search URL without clobbering existing params."""
    if filters is None:
        return url
    extra = filters.params()
    if not extra:
        return url
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    for key, value in extra.items():
        query.setdefault(key, value)
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def item_url(listing: RawListing) -> str:
    """The listing's own page. Synthesised from the id when the grid record omits it."""
    if listing.url and "/marketplace/item/" in listing.url:
        return listing.url
    return f"https://www.facebook.com/marketplace/item/{listing.fb_listing_id}/"


# --- result ------------------------------------------------------------------------------

@dataclass
class ScrapeResult:
    listings: list[RawListing] = field(default_factory=list)
    mode: Mode = "two-stage"
    index_count: int = 0
    detail_requested: list[str] = field(default_factory=list)
    detail_count: int = 0
    coverage: dict[str, SearchCoverage] = field(default_factory=dict)
    searches_failed: list[str] = field(default_factory=list)
    detail_supported: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def detail_ratio(self) -> float:
        return (len(self.detail_requested) / self.index_count) if self.index_count else 0.0

    def summary(self) -> str:
        return (
            f"{self.mode}: {self.index_count} indexed -> "
            f"{len(self.detail_requested)} detail-fetched ({self.detail_ratio:.0%})"
            + (f" · {len(self.searches_failed)} search(es) failed" if self.searches_failed else "")
            + (f" · {'; '.join(self.notes)}" if self.notes else "")
        )


# --- the stages --------------------------------------------------------------------------

def _index_input(url: str, limit: int, *, details: bool) -> dict:
    return {
        "startUrls": [{"url": url}],
        "resultsLimit": limit,
        "includeListingDetails": details,
    }


def _scan_searches(
    urls: Sequence[str], *, fetch: Fetcher, actor: str, limit: int, details: bool,
    filters: SearchFilters | None,
) -> tuple[list[RawListing], dict[str, SearchCoverage], list[str]]:
    """Run one pass over every search URL, surviving individual failures."""
    from datetime import datetime, timezone

    out: list[RawListing] = []
    coverage: dict[str, SearchCoverage] = {}
    failed: list[str] = []
    for raw_url in urls:
        url = apply_filters(raw_url, filters)
        try:
            got = fetch(_index_input(url, limit, details=details), actor)
        except Exception as exc:  # noqa: BLE001 — one bad search shouldn't sink the run
            log.warning("search_failed", url=url, error=str(exc)[:300])
            failed.append(f"{raw_url}: {exc}")
            continue
        out += got
        coverage[raw_url] = SearchCoverage(
            url=raw_url,
            last_ok_at=datetime.now(timezone.utc),
            last_count=len(got),
            # Hitting the cap means the result set was cut off, so a listing missing from
            # this scan may well still be for sale. The catalogue needs to know.
            truncated=len(got) >= limit,
        )
    return out, coverage, failed


def _fetch_details(
    listings: Sequence[RawListing], *, fetch: Fetcher, actor: str, timeout_limit: int,
) -> dict[str, RawListing]:
    """Stage B: one actor run over item URLs. Raises if the actor can't do it."""
    if not listings:
        return {}
    run_input = {
        "startUrls": [{"url": item_url(lst)} for lst in listings],
        "resultsLimit": timeout_limit,
        "includeListingDetails": True,
    }
    try:
        got = fetch(run_input, actor)
    except Exception as exc:  # noqa: BLE001 — this is the rung we're testing for support
        raise DetailStageUnsupported(str(exc)) from exc
    if not got:
        raise DetailStageUnsupported("the detail run returned no records")
    return {
        lst.fb_listing_id: lst.model_copy(update={"detail_fetched": True})
        for lst in got
        if lst.detail_fetched or lst.description
    }


def _merge(index: Sequence[RawListing], details: Mapping[str, RawListing]) -> list[RawListing]:
    """Detail records win where present; index records carry the rest."""
    return [details.get(lst.fb_listing_id, lst) for lst in index]


def select_for_detail(
    index: Sequence[RawListing],
    seen: Mapping[str, int | None],
    *,
    already_detailed: Collection[str] = (),
    cap: int = 40,
) -> list[RawListing]:
    """Which listings are worth paying to read in full.

    New or newly-cheaper first, minus anything already read. That exclusion is the point: a
    price drop on a piece we've already detailed costs zero scrape *and*, thanks to the
    catalogue, zero AI — it just re-ranks.

    Leftover budget then goes to *still-thin* listings we've seen before. Without that a
    listing cut by the cap would be stranded: next run it is neither new nor cheaper, so it
    would never qualify again and would sit on a title alone forever.
    """
    done = set(already_detailed)

    def rank(lst: RawListing) -> tuple:
        # Richest first, so if the cap bites we lose the least informative records.
        return (len(lst.photos), lst.asking_price_cents or 0)

    eligible = [
        lst for lst in index if lst.fb_listing_id not in done and not lst.detail_fetched
    ]
    actionable = {lst.fb_listing_id for lst in diff_new_and_changed(index, seen).actionable}

    fresh = sorted((l for l in eligible if l.fb_listing_id in actionable), key=rank, reverse=True)
    stale = sorted((l for l in eligible if l.fb_listing_id not in actionable), key=rank,
                   reverse=True)
    return (fresh + stale)[:cap]


def scrape(
    search_urls: Iterable[str],
    seen: Mapping[str, int | None],
    *,
    fetch: Fetcher,
    already_detailed: Collection[str] = (),
    actor: str = DEFAULT_ACTOR,
    detail_actor: str | None = None,
    results_limit: int = 60,
    detail_cap: int = 40,
    detail_supported: bool | None = None,
    filters: SearchFilters | None = None,
) -> ScrapeResult:
    """Scrape the searches for the least money that still answers today's question.

    ``detail_supported`` is the remembered verdict from a previous run: ``None`` means never
    probed (try it), ``False`` means this actor can't fetch item pages (don't pay to find
    out again), ``True`` means it can.

    The ladder, in order:

    1. **two-stage** — cheap index scan, then details for the selected ids only;
    2. **single-stage** — one pass per search with details on, i.e. the old behaviour, used
       when Stage B is unsupported;
    3. **thin-only** — index records alone, left marked as un-detailed so the catalogue
       queues them for enrichment rather than silently valuing them on a title.
    """
    urls = [u for u in search_urls if u]
    res = ScrapeResult(detail_supported=detail_supported)

    if detail_supported is False:
        # Known-unsupported: go straight to one detailed pass. Doing the index scan first
        # would just be a second bill for the same rows.
        listings, coverage, failed = _scan_searches(
            urls, fetch=fetch, actor=actor, limit=results_limit, details=True, filters=filters
        )
        res.mode = "single-stage"
        res.listings = dedup_listings(listings)
        res.index_count = len(res.listings)
        res.detail_requested = [lst.fb_listing_id for lst in res.listings if lst.detail_fetched]
        res.detail_count = len(res.detail_requested)
        res.coverage, res.searches_failed = coverage, failed
        return res

    index, coverage, failed = _scan_searches(
        urls, fetch=fetch, actor=actor, limit=results_limit, details=False, filters=filters
    )
    index = dedup_listings(index)
    res.coverage, res.searches_failed = coverage, failed
    res.index_count = len(index)
    res.listings = index

    wanted = select_for_detail(index, seen, already_detailed=already_detailed, cap=detail_cap)
    res.detail_requested = [lst.fb_listing_id for lst in wanted]
    if not wanted:
        res.detail_supported = detail_supported
        return res

    try:
        details = _fetch_details(
            wanted, fetch=fetch, actor=detail_actor or actor, timeout_limit=len(wanted),
        )
    except DetailStageUnsupported as exc:
        log.warning("detail_stage_unsupported", actor=detail_actor or actor, error=str(exc)[:200])
        res.detail_supported = False
        res.notes.append(f"detail stage unsupported ({str(exc)[:120]}); retried single-stage")
        # Rung 2: one detailed pass over the searches.
        retry, retry_cov, retry_failed = _scan_searches(
            urls, fetch=fetch, actor=actor, limit=results_limit, details=True, filters=filters
        )
        detailed = [lst for lst in retry if lst.detail_fetched]
        if detailed:
            res.mode = "single-stage"
            res.listings = dedup_listings(retry)
            res.index_count = len(res.listings)
            res.detail_requested = [lst.fb_listing_id for lst in res.listings if lst.detail_fetched]
            res.detail_count = len(res.detail_requested)
            res.coverage = retry_cov or coverage
            res.searches_failed = retry_failed
            return res
        # Rung 3: keep the thin records. They stay marked un-detailed, so they are
        # re-offered for enrichment once the actor situation is fixed.
        res.mode = "thin-only"
        res.notes.append("single-stage retry produced no detail either; kept thin records")
        res.detail_requested = []
        return res

    res.detail_supported = True
    res.detail_count = len(details)
    res.listings = _merge(index, details)
    return res


# --- probe -------------------------------------------------------------------------------

def probe(token: str, search_url: str, *, actor: str = DEFAULT_ACTOR) -> ScrapeResult:
    """Verify both stages against the live actor for a few cents' worth of credit.

    Deliberately tiny (5 index rows, 2 detail fetches) — its job is to answer "does Stage B
    work with this actor", which the catalogue then remembers.
    """
    from dealfinder.sources.apify import run_and_fetch

    def fetch(run_input: dict, actor_id: str) -> list[RawListing]:
        return run_and_fetch(run_input, token=token, actor=actor_id)

    return scrape(
        [search_url], {}, fetch=fetch, actor=actor, results_limit=5, detail_cap=2,
    )


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Probe the two-stage scrape against Apify")
    ap.add_argument("--url", default="https://www.facebook.com/marketplace/lexington/search/?query=dresser")
    ap.add_argument("--actor", default=os.getenv("APIFY_ACTOR") or DEFAULT_ACTOR)
    args = ap.parse_args(argv)

    token = (os.getenv("APIFY_TOKEN") or "").strip()
    if not token:
        print("APIFY_TOKEN is not set — nothing to probe.")
        return 2
    res = probe(token, args.url, actor=args.actor)
    print(res.summary())
    print(f"detail stage supported: {res.detail_supported}")
    for lst in res.listings[:3]:
        print(f"  {lst.fb_listing_id}  detail={lst.detail_fetched}  {lst.title[:60]}")
    return 0 if res.detail_supported else 1


if __name__ == "__main__":
    raise SystemExit(_main())
