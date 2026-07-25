"""Algorithm-agnostic enumeration and high-level scrape entry points.

Rather than trusting Facebook's ranked feed, we enumerate systematically:
category x location(+radius) x keyword x **price bands**. Partitioning the price axis
into bands defeats Facebook's implicit result-count caps and surfaces the long tail, so
we catch everything in the target window — including brand-new posts — not just what the
feed chooses to show.
"""

from __future__ import annotations

from urllib.parse import urlencode

from dealfinder.core.models import SearchTarget
from dealfinder.core.schemas import RawListing
from dealfinder.logging import get_logger
from dealfinder.scraper import parse
from dealfinder.scraper.pacing import RateGovernor, human_pause
from dealfinder.scraper.session import BrowserSession

log = get_logger(__name__)

_BASE = "https://www.facebook.com/marketplace"


def price_bands(
    min_cents: int | None, max_cents: int | None, n: int = 4
) -> list[tuple[int | None, int | None]]:
    """Split a price window into ``n`` contiguous bands (dollars).

    An open-ended window (no min/max) is returned as a single unbounded band; the caller
    still gets long-tail coverage from the seen-set diff across cycles.
    """
    if min_cents is None or max_cents is None or max_cents <= min_cents:
        return [(min_cents, max_cents)]
    lo, hi = min_cents, max_cents
    step = (hi - lo) // n
    bands: list[tuple[int | None, int | None]] = []
    cur = lo
    for i in range(n):
        band_hi = hi if i == n - 1 else cur + step
        bands.append((cur, band_hi))
        cur = band_hi
    return bands


def build_search_url(
    target: SearchTarget,
    band_min_cents: int | None,
    band_max_cents: int | None,
) -> str:
    params: dict[str, str] = {}
    if target.query:
        params["query"] = target.query
    if band_min_cents is not None:
        params["minPrice"] = str(band_min_cents // 100)
    if band_max_cents is not None:
        params["maxPrice"] = str(band_max_cents // 100)
    if target.radius_km:
        params["radius"] = str(target.radius_km)
    params["sortBy"] = "creation_time_descend"  # newest first, algo-agnostic
    location = target.location or "category"
    query = f"?{urlencode(params)}" if params else ""
    # e.g. /marketplace/nyc/search?query=dresser&minPrice=0&maxPrice=100&radius=40
    return f"{_BASE}/{location}/search{query}"


def item_url(fb_id: str) -> str:
    return f"{_BASE}/item/{fb_id}/"


async def enumerate_ids(
    target: SearchTarget, session: BrowserSession, governor: RateGovernor
) -> list[str]:
    """Return all distinct listing IDs in the target window, across price bands."""
    all_ids: list[str] = []
    seen: set[str] = set()
    for band_min, band_max in price_bands(
        target.min_price_cents, target.max_price_cents
    ):
        await governor.acquire()
        url = build_search_url(target, band_min, band_max)
        html = await session.fetch_html(url)
        for fb_id in parse.parse_search_ids(html):
            if fb_id not in seen:
                seen.add(fb_id)
                all_ids.append(fb_id)
        await human_pause()
    log.info("enumerated", target=target.name, count=len(all_ids))
    return all_ids


async def scrape_detail(
    fb_id: str, session: BrowserSession, governor: RateGovernor
) -> RawListing:
    """Fetch and parse a single listing's detail page."""
    await governor.acquire()
    url = item_url(fb_id)
    html = await session.fetch_html(url)
    return parse.parse_listing_detail(html, url=url)
