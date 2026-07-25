"""The catalogue — the thing that makes runs accumulate instead of evaporating.

Before this, each run appraised a dozen listings, rendered them, and threw the results
away; the only memory was ``{id: price}``, so a piece valued on Monday was invisible on
Thursday — skipped as "already seen" with no record of what we concluded.

The catalogue keeps, per listing, the index fields, price history, live/sold/gone state,
and **the appraisal** — the one expensive, non-reproducible artifact. It deliberately does
*not* store computed scores (priority, margin, resale, badges): those are recomputed each
run by :func:`dealfinder.engine.evaluate_piece`, so improving the ranking or resale logic
retroactively improves every piece already catalogued.

Persistence is a single JSON file committed alongside the site, because the job runs in a
stateless GitHub Action where the repo is the only durable storage.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from dealfinder.core.schemas import AppraisalResult, RawListing, RawPhoto
from dealfinder.logging import get_logger

log = get_logger(__name__)

CATALOG_VERSION = 1
_DESC_CAP = 600          # descriptions are for context, not archival
_PRICE_POINTS_CAP = 12
_PHOTO_URL_CAP = 3       # signed URLs expire; the local copies are the real record


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PricePoint(BaseModel):
    at: datetime
    cents: int | None = None


class CatalogEntry(BaseModel):
    id: str
    title: str = ""
    description: str = ""
    url: str = ""
    location_text: str = ""
    asking_price_cents: int | None = None
    was_price_cents: int | None = None
    price_history: list[PricePoint] = Field(default_factory=list)

    first_seen: datetime
    last_seen: datetime
    posted_at: datetime | None = None
    misses: int = 0                       # consecutive scans that didn't return it
    state: Literal["live", "sold", "gone"] = "live"
    sold_at: datetime | None = None
    sold_price_cents: int | None = None

    photo_rel: str | None = None          # e.g. "photos/<id>.jpg", relative to the site root
    photo_urls: list[str] = Field(default_factory=list)
    detail_fetched: bool = False

    appraisal: AppraisalResult | None = None
    appraised_at: datetime | None = None
    appraised_price_cents: int | None = None
    appraiser: str = ""

    def to_listing(self) -> RawListing:
        """Rebuild a listing good enough to re-score against today's price."""
        raw: dict = {}
        if self.was_price_cents:
            raw["_was_price_cents"] = self.was_price_cents
        return RawListing(
            fb_listing_id=self.id,
            title=self.title,
            description=self.description,
            asking_price_cents=self.asking_price_cents,
            location_text=self.location_text,
            url=self.url,
            photos=[RawPhoto(remote_url=u, position=i) for i, u in enumerate(self.photo_urls)],
            raw_json=raw,
            detail_fetched=self.detail_fetched,
            posted_at=self.posted_at,
        )


class SearchCoverage(BaseModel):
    url: str
    last_ok_at: datetime | None = None
    last_count: int = 0
    truncated: bool = True   # hit resultsLimit -> we did NOT see the whole result set


class CatalogMeta(BaseModel):
    detail_fetch_supported: bool | None = None   # None = never probed
    last_probe_at: datetime | None = None
    searches: dict[str, SearchCoverage] = Field(default_factory=dict)


class Catalog(BaseModel):
    version: int = CATALOG_VERSION
    updated_at: datetime = Field(default_factory=_now)
    meta: CatalogMeta = Field(default_factory=CatalogMeta)
    listings: dict[str, CatalogEntry] = Field(default_factory=dict)


class ObserveReport(BaseModel):
    new: int = 0
    price_drops: int = 0
    returned_to_live: int = 0
    marked_sold: int = 0
    marked_gone: int = 0
    detail_upgrades: list[str] = Field(default_factory=list)
    """Ids that went from a thin grid record to a full detail record in this scan — the one
    case where re-appraising a piece we've already valued is worth paying for."""


class PruneReport(BaseModel):
    removed_ids: list[str] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.removed_ids)


# --- persistence ----------------------------------------------------------------------

def load_catalog(path: Path) -> Catalog:
    """Load the catalogue, degrading to empty rather than killing a run."""
    path = Path(path)
    if not path.exists():
        return Catalog()
    try:
        return Catalog.model_validate_json(path.read_text())
    except (ValidationError, ValueError) as exc:
        log.warning("catalog_unreadable", path=str(path), error=str(exc)[:200])
        return Catalog()


def save_catalog(catalog: Catalog, path: Path) -> None:
    """Write with stable ordering so consecutive commits produce tight git diffs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    catalog.updated_at = _now()
    payload = catalog.model_dump(mode="json")
    payload["listings"] = dict(sorted(payload.get("listings", {}).items()))
    import json

    path.write_text(json.dumps(payload, indent=1, sort_keys=True))


def migrate_from_seen(seen: Mapping[str, int | None], *, now: datetime | None = None) -> Catalog:
    """Seed a catalogue from the old flat ledger so nothing is re-appraised needlessly."""
    now = now or _now()
    cat = Catalog()
    for listing_id, price in seen.items():
        cat.listings[listing_id] = CatalogEntry(
            id=listing_id, asking_price_cents=price, first_seen=now, last_seen=now
        )
    return cat


# --- views the rest of the pipeline consumes -------------------------------------------

def seen_view(catalog: Catalog) -> dict[str, int | None]:
    """The compatibility hinge: ``{id: price}``, exactly what selection.py already expects,
    so ``diff_new_and_changed`` and ``plan_appraisals`` need no changes at all."""
    return {e.id: e.asking_price_cents for e in catalog.listings.values()}


def detailed_ids(catalog: Catalog) -> set[str]:
    return {e.id for e in catalog.listings.values() if e.detail_fetched}


def live_entries(catalog: Catalog, *, appraised_only: bool = True) -> list[CatalogEntry]:
    out = [e for e in catalog.listings.values() if e.state == "live"]
    if appraised_only:
        out = [e for e in out if e.appraisal is not None]
    return out


def already_valued(catalog: Catalog, *, exclude: Iterable[str] = ()) -> set[str]:
    """Ids that already carry a usable appraisal, so this run must not pay to value them again.

    This is the second half of the cost fix. The seen-diff correctly treats a price drop as
    actionable — it changes what the piece is worth to *us* — but not as new information
    about the *object*, which is all the appraisal describes. So a discounted piece we have
    already valued re-ranks through :func:`dealfinder.engine.evaluate_piece` for free, and
    only pieces in ``exclude`` (typically this scan's thin-to-detailed upgrades) are re-valued.
    """
    skip = set(exclude)
    return {e.id for e in catalog.listings.values() if e.appraisal is not None} - skip


def unappraised_live(catalog: Catalog) -> list[RawListing]:
    """Pieces we know about but never valued — the backfill pool for leftover budget."""
    return [
        e.to_listing()
        for e in catalog.listings.values()
        if e.state == "live" and e.appraisal is None
    ]


# --- mutation -------------------------------------------------------------------------

def observe(
    catalog: Catalog,
    listings: Iterable[RawListing],
    *,
    now: datetime | None = None,
    coverage: Mapping[str, SearchCoverage] | None = None,
    gone_after_days: int = 14,
) -> ObserveReport:
    """Fold a scan's results into the catalogue.

    Absence is deliberately weak evidence: with a ``resultsLimit`` in play a live listing
    drops out of view routinely, so a piece is only marked gone when every search returned
    an untruncated result set AND it was missed twice — otherwise we fall back to age.
    """
    now = now or _now()
    rep = ObserveReport()
    seen_now: set[str] = set()

    for lst in listings:
        seen_now.add(lst.fb_listing_id)
        entry = catalog.listings.get(lst.fb_listing_id)
        price = lst.asking_price_cents

        if entry is None:
            entry = CatalogEntry(
                id=lst.fb_listing_id, first_seen=now, last_seen=now,
                price_history=[PricePoint(at=now, cents=price)],
            )
            catalog.listings[entry.id] = entry
            rep.new += 1
        else:
            if entry.state == "gone":
                entry.state = "live"
                rep.returned_to_live += 1
            if price is not None and entry.asking_price_cents is not None:
                if price < entry.asking_price_cents:
                    rep.price_drops += 1
                if price != entry.asking_price_cents:
                    entry.price_history.append(PricePoint(at=now, cents=price))
                    entry.price_history = entry.price_history[-_PRICE_POINTS_CAP:]

        entry.last_seen = now
        entry.misses = 0
        entry.title = lst.title or entry.title
        entry.url = lst.url or entry.url
        entry.location_text = lst.location_text or entry.location_text
        entry.posted_at = lst.posted_at or entry.posted_at
        if price is not None:
            entry.asking_price_cents = price
        was = lst.raw_json.get("_was_price_cents")
        if was:
            entry.was_price_cents = was
        if lst.description:
            entry.description = lst.description[:_DESC_CAP]
        if lst.photos:
            # Facebook's signed URLs expire within hours, so these are a stale fallback at
            # best — the copies under docs/photos/ are what the board actually renders.
            # Keeping a handful of 300-character URLs per entry is most of the file size.
            entry.photo_urls = [p.remote_url for p in lst.photos][:_PHOTO_URL_CAP]
        if lst.detail_fetched and not entry.detail_fetched:
            rep.detail_upgrades.append(entry.id)
            entry.detail_fetched = True
        if lst.is_sold:
            if entry.state != "sold":
                rep.marked_sold += 1
            entry.state = "sold"
            entry.sold_at = entry.sold_at or now
            entry.sold_price_cents = entry.sold_price_cents or entry.asking_price_cents

    # Absence handling
    fully_covered = bool(coverage) and all(not c.truncated for c in coverage.values())
    cutoff = now - timedelta(days=gone_after_days)
    for entry in catalog.listings.values():
        if entry.id in seen_now or entry.state != "live":
            continue
        entry.misses += 1
        if (fully_covered and entry.misses >= 2) or entry.last_seen < cutoff:
            entry.state = "gone"
            rep.marked_gone += 1

    if coverage:
        catalog.meta.searches.update(dict(coverage))
    return rep


def record_capability(catalog: Catalog, supported: bool | None, *, now: datetime | None = None) -> None:
    """Remember whether the actor can fetch item detail pages.

    Finding out costs a real (small) scrape, so the verdict is persisted: an actor that
    can't do it is discovered once, not once per run.
    """
    if supported is None:
        return
    catalog.meta.detail_fetch_supported = supported
    catalog.meta.last_probe_at = now or _now()


def needs_reappraisal(entry: CatalogEntry, listing: RawListing) -> bool:
    """Re-appraise only on genuinely new evidence — i.e. we previously had thin data and
    now have a description and photos. A price change alone never justifies a new AI call:
    :func:`evaluate_piece` re-scores it for free."""
    return entry.appraisal is not None and not entry.detail_fetched and listing.detail_fetched


def record_appraisals(
    catalog: Catalog,
    pieces: Iterable,           # Iterable[EvaluatedPiece] — untyped to avoid a circular import
    *,
    now: datetime | None = None,
    appraiser: str = "",
    photo_rel: Mapping[str, str] | None = None,
) -> None:
    now = now or _now()
    photo_rel = photo_rel or {}
    for piece in pieces:
        entry = catalog.listings.get(piece.listing.fb_listing_id)
        if entry is None:
            continue
        entry.appraisal = piece.appraisal
        entry.appraised_at = now
        entry.appraised_price_cents = piece.listing.asking_price_cents
        entry.appraiser = appraiser
        rel = photo_rel.get(entry.id)
        if rel:
            entry.photo_rel = rel


def prune(
    catalog: Catalog,
    *,
    now: datetime | None = None,
    gone_unappraised_days: int = 30,
    gone_days: int = 90,
    sold_days: int = 180,
    max_entries: int = 1200,
) -> PruneReport:
    """Bound the file. Never drops a live entry; returns ids so photos are cleaned too."""
    now = now or _now()
    rep = PruneReport()

    def drop(entry: CatalogEntry) -> bool:
        age = (now - entry.last_seen).days
        if entry.state == "gone":
            if entry.appraisal is None and age > gone_unappraised_days:
                return True
            return age > gone_days
        if entry.state == "sold":
            return age > sold_days
        return False

    for entry in list(catalog.listings.values()):
        if drop(entry):
            rep.removed_ids.append(entry.id)
            del catalog.listings[entry.id]

    if len(catalog.listings) > max_entries:
        expendable = sorted(
            (e for e in catalog.listings.values() if e.state != "live"),
            key=lambda e: e.last_seen,
        )
        for entry in expendable:
            if len(catalog.listings) <= max_entries:
                break
            rep.removed_ids.append(entry.id)
            del catalog.listings[entry.id]

    return rep
