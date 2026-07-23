"""Data-access helpers: upserts, dedup, and common queries.

Every layer that touches the DB goes through these functions rather than issuing
ad-hoc queries, so dedup and price-history rules live in exactly one place.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from dealfinder.core.enums import ListingStatus, ValuationTier
from dealfinder.core.models import (
    Listing,
    ListingPhoto,
    PriceHistory,
    SeenSet,
    Valuation,
)
from dealfinder.core.schemas import RawListing


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Seen-set dedup -------------------------------------------------------

def filter_new_ids(session: Session, target_id: int, fb_ids: list[str]) -> list[str]:
    """Return the subset of ``fb_ids`` not yet recorded in the seen-set for this target."""
    if not fb_ids:
        return []
    rows = session.execute(
        select(SeenSet.fb_listing_id).where(
            SeenSet.search_target_id == target_id,
            SeenSet.fb_listing_id.in_(fb_ids),
        )
    ).scalars().all()
    seen = set(rows)
    return [i for i in fb_ids if i not in seen]


def record_seen(session: Session, target_id: int, fb_ids: list[str]) -> None:
    existing = set(
        session.execute(
            select(SeenSet.fb_listing_id).where(
                SeenSet.search_target_id == target_id,
                SeenSet.fb_listing_id.in_(fb_ids or [""]),
            )
        ).scalars().all()
    )
    for fb_id in fb_ids:
        if fb_id not in existing:
            session.add(SeenSet(search_target_id=target_id, fb_listing_id=fb_id))


# --- Listing upsert -------------------------------------------------------

def upsert_listing(
    session: Session, raw: RawListing, target_id: int | None
) -> Listing:
    """Insert a new listing or refresh an existing one; track price changes."""
    listing = session.get(Listing, raw.fb_listing_id)
    now = _utcnow()

    if listing is None:
        listing = Listing(
            id=raw.fb_listing_id,
            search_target_id=target_id,
            status=ListingStatus.NEW,
            first_seen_at=now,
        )
        session.add(listing)

    listing.title = raw.title
    listing.description = raw.description
    listing.currency = raw.currency
    listing.location_text = raw.location_text
    listing.seller_name = raw.seller_name
    listing.seller_profile_url = raw.seller_profile_url
    listing.url = raw.url
    listing.raw_json = raw.raw_json
    listing.last_seen_at = now

    # Price + history
    if raw.asking_price_cents is not None:
        if listing.asking_price_cents != raw.asking_price_cents:
            session.add(
                PriceHistory(
                    listing_id=listing.id,
                    price_cents=raw.asking_price_cents,
                    observed_at=now,
                )
            )
        listing.asking_price_cents = raw.asking_price_cents

    # Photos: only rebuild the rows when the set of remote URLs actually changed, so a
    # re-scrape doesn't discard already-downloaded local_path/sha256 (finding B7).
    existing_urls = [p.remote_url for p in listing.photos]
    incoming_urls = [p.remote_url for p in raw.photos]
    if existing_urls != incoming_urls:
        listing.photos.clear()
        for p in raw.photos:
            listing.photos.append(
                ListingPhoto(remote_url=p.remote_url, position=p.position)
            )

    session.flush()
    return listing


def add_valuation(session: Session, valuation: Valuation) -> Valuation:
    session.add(valuation)
    session.flush()
    return valuation


def add_appraisal(session: Session, valuation: Valuation) -> Valuation:
    """Add an appraise-tier valuation and mark it the single current one for its listing,
    demoting any prior current appraisal (finding B3)."""
    session.execute(
        update(Valuation)
        .where(
            Valuation.listing_id == valuation.listing_id,
            Valuation.tier == ValuationTier.APPRAISE,
            Valuation.is_current.is_(True),
        )
        .values(is_current=False)
    )
    valuation.is_current = True
    session.add(valuation)
    session.flush()
    return valuation


def current_valuation(session: Session, listing_id: str) -> Valuation | None:
    """The current appraisal-tier valuation for a listing."""
    return session.execute(
        select(Valuation).where(
            Valuation.listing_id == listing_id,
            Valuation.tier == ValuationTier.APPRAISE,
            Valuation.is_current.is_(True),
        )
    ).scalars().first()
