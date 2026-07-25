"""Cost-control funnel: turn many overlapping scrapes into the smallest paid workload.

Three distinct leaks this plugs, in order:

1. **Cross-scrape overlap** — searching ``dresser``, ``mcm``, ``walnut`` returns the same
   pieces. :func:`dedup_listings` unions every search into one block keyed by listing id,
   so a piece is only ever considered once per run no matter how many searches hit it.

2. **Cross-run repeats** — Monday's 1,000 results shouldn't be re-appraised on Thursday.
   :func:`diff_new_and_changed` compares against a *seen ledger* and advances only listings
   that are genuinely new or whose price has dropped (a price drop is itself a buy signal).

3. **Appraisal blowout** — even the new pieces are capped. :func:`select_for_appraisal`
   ranks the pre-screen survivors and forwards only the top ``N`` plus ``K`` "wildcards"
   (ambiguous/mistitled pieces with no keyword signal but real photos) to the paid vision
   model.

Everything here is pure functions over plain data — no I/O, no AI — so it is cheap to test
and cheap to reason about. The seen ledger is a plain ``{listing_id: last_price_cents}``
map; persistence wires into the repository at the pipeline layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from dealfinder.core.schemas import RawListing
from dealfinder.prescreen import PreScreenResult, prescreen
from dealfinder.verticals import DEFAULT_VERTICAL, Vertical


def _richness(listing: RawListing) -> tuple[int, int]:
    """How much a record gives the appraiser: (has_price, photo_count). Higher = keep."""
    return (1 if listing.asking_price_cents is not None else 0, len(listing.photos))


def dedup_listings(listings: Iterable[RawListing]) -> list[RawListing]:
    """Collapse the union of all searches to one record per listing id.

    When the same id appears in several searches (thin record from one, full from another),
    the *richest* record wins — the one with a price and the most photos.
    """
    best: dict[str, RawListing] = {}
    for lst in listings:
        cur = best.get(lst.fb_listing_id)
        if cur is None or _richness(lst) > _richness(cur):
            best[lst.fb_listing_id] = lst
    return list(best.values())


@dataclass
class DiffResult:
    new: list[RawListing] = field(default_factory=list)
    price_dropped: list[RawListing] = field(default_factory=list)
    unchanged: list[RawListing] = field(default_factory=list)

    @property
    def actionable(self) -> list[RawListing]:
        """Listings worth spending on this run: brand-new, or newly cheaper."""
        return self.new + self.price_dropped


def diff_new_and_changed(
    listings: Iterable[RawListing],
    seen: Mapping[str, int | None],
) -> DiffResult:
    """Split listings by what's worth paying to evaluate, given what we've already seen.

    ``seen`` maps ``listing_id -> last_observed_price_cents`` (None if price was unknown).
    """
    out = DiffResult()
    for lst in listings:
        if lst.fb_listing_id not in seen:
            out.new.append(lst)
            continue
        prev = seen[lst.fb_listing_id]
        cur = lst.asking_price_cents
        if prev is not None and cur is not None and cur < prev:
            out.price_dropped.append(lst)
        else:
            out.unchanged.append(lst)
    return out


def update_seen(
    seen: Mapping[str, int | None],
    listings: Iterable[RawListing],
) -> dict[str, int | None]:
    """Return a new seen-ledger folding in this run's observed prices."""
    merged = dict(seen)
    for lst in listings:
        merged[lst.fb_listing_id] = lst.asking_price_cents
    return merged


@dataclass
class Selection:
    """The capped set that will actually be appraised, plus what it cost us to get here."""

    to_appraise: list[RawListing] = field(default_factory=list)
    strong: list[RawListing] = field(default_factory=list)
    wildcards: list[RawListing] = field(default_factory=list)
    dropped_by_prescreen: int = 0
    over_cap: int = 0  # strong candidates that exceeded top_n and were left out


def select_for_appraisal(
    listings: Iterable[RawListing],
    *,
    vertical: Vertical = DEFAULT_VERTICAL,
    top_n: int = 20,
    wildcards: int = 5,
) -> Selection:
    """Pre-screen, then forward only the best ``top_n`` + ``wildcards`` to the paid model.

    *Strong* = clear keyword/maker signal, ranked by signal strength then photo count.
    *Wildcards* = kept-but-unsignalled pieces (a seller who mistitled a real find). These
    are where vision beats keyword tools, so we always spend a few appraisals probing them.
    """
    scored: list[tuple[RawListing, PreScreenResult]] = []
    dropped = 0
    for lst in listings:
        ps = prescreen(lst, vertical)
        if ps.keep:
            scored.append((lst, ps))
        else:
            dropped += 1

    strong_ranked = sorted(
        (x for x in scored if x[1].score >= 1),
        key=lambda x: (x[1].score, len(x[0].photos)),
        reverse=True,
    )
    wildcard_pool = sorted(
        (x for x in scored if x[1].score == 0),
        key=lambda x: (len(x[0].photos), x[0].asking_price_cents or 0),
        reverse=True,
    )

    strong = [lst for lst, _ in strong_ranked[:top_n]]
    picks = [lst for lst, _ in wildcard_pool[:wildcards]]

    return Selection(
        to_appraise=strong + picks,
        strong=strong,
        wildcards=picks,
        dropped_by_prescreen=dropped,
        over_cap=max(0, len(strong_ranked) - top_n),
    )


@dataclass
class AppraisalPlan:
    """End-to-end cost-control result — the paid workload plus a full audit of what was cut."""

    to_appraise: list[RawListing] = field(default_factory=list)
    strong: list[RawListing] = field(default_factory=list)
    wildcards: list[RawListing] = field(default_factory=list)
    total_scraped: int = 0
    after_dedup: int = 0
    new: int = 0
    price_dropped: int = 0
    skipped_seen: int = 0
    dropped_by_prescreen: int = 0
    over_cap: int = 0

    def summary(self) -> str:
        return (
            f"{self.total_scraped} scraped -> {self.after_dedup} after dedup -> "
            f"{self.new} new + {self.price_dropped} price-drops "
            f"({self.skipped_seen} already-seen, skipped) -> "
            f"{len(self.to_appraise)} appraised "
            f"({len(self.strong)} strong + {len(self.wildcards)} wildcards; "
            f"{self.dropped_by_prescreen} junked, {self.over_cap} over cap)"
        )


def plan_appraisals(
    listings: Iterable[RawListing],
    seen: Mapping[str, int | None],
    *,
    vertical: Vertical = DEFAULT_VERTICAL,
    top_n: int = 20,
    wildcards: int = 5,
) -> AppraisalPlan:
    """Run the whole cost-control pipeline: dedup -> seen-diff -> pre-screen -> cap.

    Returns the exact (small) set to pay for, plus counts at every stage so the dashboard
    can show what was skipped rather than silently capping.
    """
    raw = list(listings)
    deduped = dedup_listings(raw)
    diff = diff_new_and_changed(deduped, seen)
    sel = select_for_appraisal(
        diff.actionable, vertical=vertical, top_n=top_n, wildcards=wildcards
    )
    return AppraisalPlan(
        to_appraise=sel.to_appraise,
        strong=sel.strong,
        wildcards=sel.wildcards,
        total_scraped=len(raw),
        after_dedup=len(deduped),
        new=len(diff.new),
        price_dropped=len(diff.price_dropped),
        skipped_seen=len(diff.unchanged),
        dropped_by_prescreen=sel.dropped_by_prescreen,
        over_cap=sel.over_cap,
    )
