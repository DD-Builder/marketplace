"""The auction catalogue — bid history as memory, ended lots as calibration data.

The Marketplace catalogue answers "have we valued this before?". The auction catalogue
has a second job the fixed-price side never needed: **price action over time**. An
auction lot's current bid is nearly meaningless on day one and decisive in the last
hour, so every scan appends a :class:`BidPoint` and the pipeline reasons over the curve,
not the number.

The third job is closing the loop. When a watched lot ends, its final price is captured
next to the last price we saw ~24 hours out — one observed ``(T-24h, final)`` pair. Those
pairs are what let :mod:`dealfinder.auctions.bidding` *learn* how much of the money
arrives in the endgame on this site, instead of forever guessing from a prior.

Persistence mirrors the main catalogue: one JSON file committed with the site, because
the pipeline runs in a stateless GitHub Action and the repo is the only durable storage.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from dealfinder.core.schemas import AppraisalResult, RawListing, RawPhoto
from dealfinder.logging import get_logger
from dealfinder.sources.ebth import AuctionItem

log = get_logger(__name__)

AUCTION_CATALOG_VERSION = 1

#: Hours before close that count as the endgame — the window where bids actually move
#: and where the board flips a lot from "watch" to "act".
ENDGAME_HOURS = 24

#: Snapshots per lot. Hourly scans over a week-long auction would be ~170 points of
#: mostly nothing; unchanged observations refresh the last point instead of appending,
#: so the cap in practice holds discovery, every real change, and the dense endgame.
_BID_POINTS_CAP = 96
_DESC_CAP = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BidPoint(BaseModel):
    at: datetime
    bid_cents: int | None = None
    bid_count: int | None = None


class AuctionEntry(BaseModel):
    id: str
    title: str = ""
    description: str = ""
    url: str = ""
    photo_urls: list[str] = Field(default_factory=list)
    photo_rel: str | None = None
    extra_photo_rels: list[str] = Field(default_factory=list)

    first_seen: datetime
    last_seen: datetime
    ends_at: datetime | None = None
    #: live -> ending (inside ENDGAME_HOURS) -> ended. ``gone`` = vanished unresolved.
    state: Literal["live", "ending", "ended", "gone"] = "live"

    #: Which :mod:`dealfinder.verticals` category surfaced this lot — EBTH sells far more
    #: than furniture (jewelry, silver, coins, watches, rugs...), and a lot found by a
    #: jewelry search must be pre-screened and appraised against jewelry's own rules, not
    #: whatever vertical the run happens to default to. Empty = pre-dates this field;
    #: ``get_vertical("")`` falls back to furniture, matching the original single-vertical
    #: behaviour so old catalogue entries don't break.
    vertical: str = ""

    bid_history: list[BidPoint] = Field(default_factory=list)
    current_bid_cents: int | None = None
    bid_count: int | None = None

    #: What the lot actually closed at — the ground truth the max-bid math is graded on.
    final_price_cents: int | None = None
    #: The last bid we observed at least ~ENDGAME_HOURS before close, captured when the
    #: lot is finalized. (final / t24) pairs across ended lots are the calibration set.
    t24_bid_cents: int | None = None

    #: Passed the quality gate — worth snapshotting every run and appraising once.
    watch: bool = False
    appraisal: AppraisalResult | None = None
    appraised_at: datetime | None = None
    appraiser: str = ""
    appraised_with_photos: bool = False

    def hours_left(self, now: datetime | None = None) -> float | None:
        if self.ends_at is None:
            return None
        return (self.ends_at - (now or _now())).total_seconds() / 3600

    def to_listing(self) -> RawListing:
        """Bridge into the shared appraisal machinery.

        ``asking_price_cents`` is deliberately None: an auction's current bid is not a
        seller's estimate of value, and handing the appraiser a transient $12 opening
        bid as "the asking price" would anchor the valuation to noise.
        """
        photos = [RawPhoto(remote_url=u, position=i) for i, u in enumerate(self.photo_urls)]
        if not photos and self.photo_rel:
            photos = [RawPhoto(remote_url=f"local:{self.photo_rel}", position=0)]
        return RawListing(
            fb_listing_id=self.id,
            title=self.title,
            description=self.description,
            asking_price_cents=None,
            url=self.url,
            photos=photos,
            detail_fetched=True,
        )


class AuctionCatalog(BaseModel):
    version: int = AUCTION_CATALOG_VERSION
    updated_at: datetime = Field(default_factory=_now)
    lots: dict[str, AuctionEntry] = Field(default_factory=dict)
    #: When discovery (search-page fetch) last ran, so an hourly workflow can snapshot
    #: cheaply every run but only trawl for new lots on its own slower cadence.
    last_discovery_at: datetime | None = None


class AuctionObserveReport(BaseModel):
    new: int = 0
    snapshots: int = 0
    entered_endgame: list[str] = Field(default_factory=list)
    finalized: list[str] = Field(default_factory=list)
    marked_gone: int = 0


# --- persistence ------------------------------------------------------------------------

class AuctionCatalogCorrupt(RuntimeError):
    """Same policy as the main catalogue: a damaged file aborts rather than letting the
    next save overwrite the bid histories and appraisals with a blank slate."""

    def __init__(self, path: Path, backup: Path, reason: str) -> None:
        self.path, self.backup, self.reason = path, backup, reason
        super().__init__(
            f"{path} is unreadable ({reason}). A copy was saved to {backup.name}; "
            "inspect before re-running — a fresh start would forget every bid history "
            "and re-buy every appraisal."
        )


def load_auction_catalog(path: Path) -> AuctionCatalog:
    path = Path(path)
    if not path.exists():
        return AuctionCatalog()
    text = path.read_text(encoding="utf-8")
    try:
        return AuctionCatalog.model_validate_json(text)
    except (ValidationError, ValueError) as exc:
        backup = path.with_name(
            f"{path.name}.corrupt-{_now().strftime('%Y%m%dT%H%M%SZ')}"
        )
        backup.write_text(text, encoding="utf-8")
        log.error("auction_catalog_corrupt", path=str(path), backup=str(backup))
        raise AuctionCatalogCorrupt(path, backup, str(exc)[:200]) from exc


def save_auction_catalog(catalog: AuctionCatalog, path: Path) -> None:
    import json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    catalog.updated_at = _now()
    payload = catalog.model_dump(mode="json")
    payload["lots"] = dict(sorted(payload.get("lots", {}).items()))
    path.write_text(json.dumps(payload, indent=1, sort_keys=True))


# --- observation ------------------------------------------------------------------------

def _append_point(entry: AuctionEntry, item: AuctionItem, now: datetime) -> None:
    """Record a sighting. Unchanged observations refresh the last point's timestamp
    rather than appending, so the history holds *changes* plus a liveness heartbeat —
    the curve stays legible and a week of quiet doesn't crowd out the endgame."""
    point = BidPoint(at=now, bid_cents=item.current_bid_cents, bid_count=item.bid_count)
    hist = entry.bid_history
    if hist and hist[-1].bid_cents == point.bid_cents \
            and hist[-1].bid_count == point.bid_count:
        hist[-1].at = now
    else:
        hist.append(point)
        if len(hist) > _BID_POINTS_CAP:
            # Never drop the first point (the opening-price anchor); thin after it.
            del hist[1]
    if item.current_bid_cents is not None:
        entry.current_bid_cents = item.current_bid_cents
    if item.bid_count is not None:
        entry.bid_count = item.bid_count


def bid_near(entry: AuctionEntry, at: datetime) -> int | None:
    """The lot's bid as of ``at`` — the latest snapshot at or before it, else the
    earliest after (a lot discovered inside the endgame has nothing earlier)."""
    before = [p for p in entry.bid_history if p.at <= at and p.bid_cents is not None]
    if before:
        return before[-1].bid_cents
    after = [p for p in entry.bid_history if p.bid_cents is not None]
    return after[0].bid_cents if after else None


def observe_auctions(
    catalog: AuctionCatalog,
    items: Iterable[AuctionItem],
    *,
    now: datetime | None = None,
) -> AuctionObserveReport:
    """Fold a scan into the catalogue: upsert lots, append snapshots, advance states."""
    now = now or _now()
    rep = AuctionObserveReport()

    for item in items:
        if not item.item_id:
            continue
        entry = catalog.lots.get(item.item_id)
        if entry is None:
            entry = AuctionEntry(
                id=item.item_id, first_seen=now, last_seen=now,
            )
            catalog.lots[entry.id] = entry
            rep.new += 1
        entry.last_seen = now
        entry.title = item.title or entry.title
        entry.url = item.url or entry.url
        if item.description:
            entry.description = item.description[:_DESC_CAP]
        if item.photo_urls:
            entry.photo_urls = item.photo_urls[:6]
        if item.ends_at is not None:
            entry.ends_at = item.ends_at
        _append_point(entry, item, now)
        rep.snapshots += 1

        if item.is_ended or (entry.ends_at is not None and entry.ends_at <= now):
            _finalize(entry, now=now, rep=rep)

    # State transitions driven by the clock, not just by sightings.
    for entry in catalog.lots.values():
        left = entry.hours_left(now)
        if entry.state == "live" and left is not None and left <= ENDGAME_HOURS:
            entry.state = "ending"
            rep.entered_endgame.append(entry.id)
        elif entry.state in ("live", "ending") and left is not None and left <= 0:
            _finalize(entry, now=now, rep=rep)
    return rep


def _finalize(entry: AuctionEntry, *, now: datetime, rep: AuctionObserveReport) -> None:
    """Close the books on a lot: last observed bid becomes the (approximate) final price,
    and the T-24h snapshot is frozen for calibration.

    "Approximate" is honest: an hourly scanner's last look can trail the true hammer
    price. The calibration therefore learns a *floor* on the endgame multiplier —
    conservative in exactly the direction that keeps max-bid advice safe.
    """
    if entry.state == "ended":
        return
    entry.state = "ended"
    entry.final_price_cents = entry.current_bid_cents
    if entry.ends_at is not None:
        entry.t24_bid_cents = bid_near(
            entry, entry.ends_at - timedelta(hours=ENDGAME_HOURS)
        )
    rep.finalized.append(entry.id)


def record_auction_appraisal(
    entry: AuctionEntry,
    appraisal: AppraisalResult,
    *,
    appraiser: str,
    with_photos: bool,
    now: datetime | None = None,
) -> None:
    entry.appraisal = appraisal
    entry.appraised_at = now or _now()
    entry.appraiser = appraiser
    entry.appraised_with_photos = with_photos


# --- views ------------------------------------------------------------------------------

def watched(catalog: AuctionCatalog) -> list[AuctionEntry]:
    return [e for e in catalog.lots.values() if e.watch and e.state in ("live", "ending")]


def ending_soon(catalog: AuctionCatalog) -> list[AuctionEntry]:
    out = [e for e in catalog.lots.values() if e.watch and e.state == "ending"]
    return sorted(out, key=lambda e: e.ends_at or datetime.max.replace(tzinfo=timezone.utc))


def comparable_closes(
    catalog: AuctionCatalog, entry: AuctionEntry, *, limit: int = 60
) -> list[tuple[datetime, int]]:
    """(closed_at, final price) for ended lots in the same category, oldest first.

    This is the only *honest* price history available to this tool. There is no public
    feed of long-run realised auction prices — eBay's sold-data API is a closed limited
    release and EBTH publishes no results archive — so rather than draw a fabricated
    trend line on a page whose entire job is deciding what to pay, the chart plots what
    this tracker has actually watched close. It starts empty and thickens every week the
    hourly job runs; the board says so plainly rather than implying a trend from three
    points.
    """
    want = entry.vertical or ""
    out = [
        (e.ends_at or e.last_seen, e.final_price_cents)
        for e in catalog.lots.values()
        if e.state == "ended"
        and e.final_price_cents
        and e.id != entry.id
        and (not want or e.vertical == want)
    ]
    out.sort(key=lambda pair: pair[0])
    return out[-limit:]


def calibration_pairs(catalog: AuctionCatalog) -> list[tuple[int, int]]:
    """(bid at T-24h, final price) for every ended lot where both are known and the
    endgame actually happened (final >= t24 > 0)."""
    pairs = []
    for e in catalog.lots.values():
        if e.state != "ended" or not e.final_price_cents or not e.t24_bid_cents:
            continue
        if e.t24_bid_cents > 0 and e.final_price_cents >= e.t24_bid_cents:
            pairs.append((e.t24_bid_cents, e.final_price_cents))
    return pairs


def unappraised_watch(
    catalog: AuctionCatalog,
    *,
    within_days: float | None = None,
    now: datetime | None = None,
) -> list[AuctionEntry]:
    """Watch-listed lots we haven't valued yet, most-urgent close first.

    ``within_days`` restricts this to lots actually closing soon. Valuation is the only
    expensive step in the whole pipeline, and a lot that closes next week will have its
    bid move many times before any decision is due — so spending an appraisal on it now
    buys nothing that spending one in two days wouldn't. A lot with no end time is
    excluded under a window, since "unknown" is not "imminent".
    """
    out = [
        e for e in catalog.lots.values()
        if e.watch and e.state in ("live", "ending") and e.appraisal is None
    ]
    if within_days is not None:
        now = now or _now()
        cutoff = now + timedelta(days=within_days)
        out = [e for e in out if e.ends_at is not None and e.ends_at <= cutoff]
    return sorted(out, key=lambda e: e.ends_at or datetime.max.replace(tzinfo=timezone.utc))


def snapshot_due(
    catalog: AuctionCatalog, *, now: datetime | None = None, quiet_hours: float = 6.0
) -> list[AuctionEntry]:
    """Which lots deserve an item-page fetch this run.

    Endgame lots always — that window is the entire point of the tracker. Everything
    else only after ``quiet_hours`` of silence: a lot closing on Saturday doesn't need
    Tuesday's hourly attention, and the fetch budget it frees is what lets the endgame
    stay hourly for free.
    """
    now = now or _now()
    due = []
    for e in catalog.lots.values():
        if not e.watch or e.state not in ("live", "ending"):
            continue
        left = e.hours_left(now)
        if e.state == "ending" or (left is not None and left <= ENDGAME_HOURS):
            due.append(e)
        elif (now - e.last_seen).total_seconds() / 3600 >= quiet_hours:
            due.append(e)
    return sorted(due, key=lambda e: e.ends_at or datetime.max.replace(tzinfo=timezone.utc))


def prune_auctions(
    catalog: AuctionCatalog,
    *,
    now: datetime | None = None,
    ended_keep_days: int = 180,
    gone_after_days: int = 14,
    unwatched_days: int = 21,
    max_lots: int = 1500,
) -> list[str]:
    """Bound the file; return removed ids so photo files are cleaned too.

    Ended lots are kept for months deliberately — they are the calibration set, and
    each one carries only a few numbers once its photos are gone. Unwatched live lots
    that stopped appearing in searches just age out; nothing was invested in them.
    """
    now = now or _now()
    removed: list[str] = []

    for entry in list(catalog.lots.values()):
        age_days = (now - entry.last_seen).days
        drop = False
        if entry.state == "ended":
            drop = age_days > ended_keep_days
        elif entry.state == "gone":
            drop = age_days > gone_after_days
        elif not entry.watch:
            drop = age_days > unwatched_days
        elif entry.ends_at is None and age_days > gone_after_days:
            # Watched but end-date-less and unseen for two weeks: the lot page stopped
            # parsing or the lot vanished. Either way there is nothing to track.
            entry.state = "gone"
        if drop:
            removed.append(entry.id)
            del catalog.lots[entry.id]

    # Ended lots keep numbers, not pictures.
    for entry in catalog.lots.values():
        if entry.state == "ended" and (now - entry.last_seen).days > 7:
            entry.photo_rel = None
            entry.extra_photo_rels = []
            entry.photo_urls = []

    if len(catalog.lots) > max_lots:
        expendable = sorted(
            (e for e in catalog.lots.values() if e.state in ("ended", "gone")),
            key=lambda e: e.last_seen,
        )
        for entry in expendable:
            if len(catalog.lots) <= max_lots:
                break
            removed.append(entry.id)
            del catalog.lots[entry.id]
    return removed
