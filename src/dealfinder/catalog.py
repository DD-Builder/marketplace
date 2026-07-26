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
    #: Additional gallery shots ("photos/<id>_1.jpg", ...). The scrape already pays for up
    #: to three photos per listing; throwing two of them away was pure waste, and judging
    #: furniture condition from one 100-px thumbnail is how you drive out for a piece with
    #: a smashed side you were never shown.
    extra_photo_rels: list[str] = Field(default_factory=list)
    photo_urls: list[str] = Field(default_factory=list)
    detail_fetched: bool = False

    appraisal: AppraisalResult | None = None
    appraised_at: datetime | None = None
    appraised_price_cents: int | None = None
    appraiser: str = ""
    #: Whether the appraiser could actually see the piece. A text-only valuation is a
    #: guess from a title, and the model says so by hedging its confidence — so it must be
    #: redone once photos exist, or the first thin valuation is locked in forever.
    appraised_with_photos: bool = False

    def to_listing(self) -> RawListing:
        """Rebuild a listing good enough to re-score against today's price."""
        raw: dict = {}
        if self.was_price_cents:
            raw["_was_price_cents"] = self.was_price_cents
        photos = [RawPhoto(remote_url=u, position=i) for i, u in enumerate(self.photo_urls)]
        if not photos and self.photo_rel:
            # The CDN links expired, but a committed copy exists on disk. That satisfies
            # "we can see this piece" for the pre-screen; the downloader skips non-http
            # URLs and the appraiser is handed the local file directly.
            photos = [RawPhoto(remote_url=f"local:{self.photo_rel}", position=0)]
        return RawListing(
            fb_listing_id=self.id,
            title=self.title,
            description=self.description,
            asking_price_cents=self.asking_price_cents,
            location_text=self.location_text,
            url=self.url,
            photos=photos,
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

class CatalogCorrupt(RuntimeError):
    """The catalogue file exists but can't be parsed.

    Deliberately fatal. The old behaviour — degrade to an empty catalogue — meant the
    next ``save_catalog`` overwrote the damaged file with a blank one, silently destroying
    every stored appraisal (the one artifact that costs real money to reproduce). Now the
    damaged file is copied aside and the run aborts, so a human decides.
    """

    def __init__(self, path: Path, backup: Path, reason: str) -> None:
        self.path, self.backup, self.reason = path, backup, reason
        super().__init__(
            f"{path} is unreadable ({reason}). A copy was saved to {backup.name}; "
            "the original was left in place. Do not re-run until it's inspected — "
            "a run would otherwise start from an empty catalogue and re-buy every appraisal."
        )


def load_catalog(path: Path) -> Catalog:
    """Load the catalogue. A missing file is a fresh start; a broken one is an abort."""
    path = Path(path)
    if not path.exists():
        return Catalog()
    text = path.read_text(encoding="utf-8")

    def _quarantine(reason: str) -> CatalogCorrupt:
        backup = path.with_name(
            f"{path.name}.corrupt-{_now().strftime('%Y%m%dT%H%M%SZ')}"
        )
        backup.write_text(text, encoding="utf-8")
        log.error("catalog_corrupt", path=str(path), backup=str(backup), reason=reason[:200])
        return CatalogCorrupt(path, backup, reason[:200])

    try:
        import json

        version = json.loads(text).get("version", CATALOG_VERSION)
    except (json.JSONDecodeError, AttributeError) as exc:
        raise _quarantine(str(exc)) from exc
    if isinstance(version, int) and version > CATALOG_VERSION:
        # Written by a newer schema than this code knows. Loading it through the old
        # model could drop fields and then persist the loss.
        raise _quarantine(f"catalogue version {version} is newer than {CATALOG_VERSION}")
    try:
        return Catalog.model_validate_json(text)
    except (ValidationError, ValueError) as exc:
        raise _quarantine(str(exc)) from exc


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


def blind_appraisals(catalog: Catalog) -> set[str]:
    """Pieces valued without ever being seen — first in line when photos become available."""
    return {
        e.id for e in catalog.listings.values()
        if e.appraisal is not None and not e.appraised_with_photos
    }


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
    absence_evidence: bool = True,
) -> ObserveReport:
    """Fold a scan's results into the catalogue.

    Absence is deliberately weak evidence: with a ``resultsLimit`` in play a live listing
    drops out of view routinely, so a piece is only marked gone when every search returned
    an untruncated result set AND it was missed twice — otherwise we fall back to age.

    ``absence_evidence=False`` says this batch carries no absence information at all — a
    recovered dataset or a local JSON export mentions the listings it mentions and implies
    nothing about the rest. Presence is still recorded; misses and retirement are not.
    """
    now = now or _now()
    rep = ObserveReport()
    seen_now: set[str] = set()

    for lst in listings:
        seen_now.add(lst.fb_listing_id)
        entry = catalog.listings.get(lst.fb_listing_id)
        price = lst.asking_price_cents
        # When the listing was *confirmed present* — for recovered data, older than now.
        seen_at = lst.observed_at or now

        if entry is None:
            entry = CatalogEntry(
                id=lst.fb_listing_id, first_seen=seen_at, last_seen=seen_at,
                price_history=[PricePoint(at=now, cents=price)],
            )
            catalog.listings[entry.id] = entry
            rep.new += 1
        else:
            if entry.state == "gone":
                entry.state = "live"
                rep.returned_to_live += 1
            elif (
                entry.state == "sold"
                and lst.is_sold is False
                and seen_at > (entry.sold_at or entry.first_seen)
            ):
                # Facebook's isSold flag also covers "pending", which routinely reverts.
                # Only an explicit fresh is_sold=False re-lists it — mere absence of the
                # flag (or an old recovered record) is not evidence it came back.
                entry.state = "live"
                entry.sold_at = None
                entry.sold_price_cents = None
                rep.returned_to_live += 1
            if price is not None and entry.asking_price_cents is not None:
                if price < entry.asking_price_cents:
                    rep.price_drops += 1
                if price != entry.asking_price_cents:
                    entry.price_history.append(PricePoint(at=now, cents=price))
                    entry.price_history = entry.price_history[-_PRICE_POINTS_CAP:]

        # Never move last_seen backwards past a fresher sighting.
        entry.last_seen = max(entry.last_seen, seen_at) if entry.last_seen else seen_at
        entry.first_seen = min(entry.first_seen, seen_at) if entry.first_seen else seen_at
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

    # Absence handling — only when this batch is a real scan of the market. A failed
    # search records itself in `coverage` as truncated, which keeps fully_covered False:
    # a listing can't be retired on the strength of a search that never ran.
    if absence_evidence:
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


def needs_reappraisal(
    entry: CatalogEntry, listing: RawListing, *, has_photos: bool = False
) -> bool:
    """Re-appraise only on genuinely new evidence.

    Two cases qualify, and only two:

    * we had a thin grid record and now have a description and gallery;
    * we valued it blind and can now show the model a photograph.

    A price change never qualifies — :func:`evaluate_piece` re-scores it for free.
    """
    if entry.appraisal is None:
        return False
    if not entry.detail_fetched and listing.detail_fetched:
        return True
    return has_photos and not entry.appraised_with_photos


def record_appraisals(
    catalog: Catalog,
    pieces: Iterable,           # Iterable[EvaluatedPiece] — untyped to avoid a circular import
    *,
    now: datetime | None = None,
    appraiser: str = "",
    photo_rel: Mapping[str, str] | None = None,
    saw_photos: Iterable[str] = (),
) -> None:
    now = now or _now()
    photo_rel = photo_rel or {}
    seen_photos = set(saw_photos)
    for piece in pieces:
        entry = catalog.listings.get(piece.listing.fb_listing_id)
        if entry is None:
            continue
        entry.appraisal = piece.appraisal
        entry.appraised_at = now
        entry.appraised_price_cents = piece.listing.asking_price_cents
        entry.appraiser = appraiser
        entry.appraised_with_photos = piece.listing.fb_listing_id in seen_photos
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
