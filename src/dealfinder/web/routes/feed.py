"""Deal feed — listings sorted by computed deal_score."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dealfinder.core.db import get_db
from dealfinder.core.enums import ListingStatus, ValuationTier
from dealfinder.core.models import Listing, Valuation
from dealfinder.web.deps import templates

router = APIRouter()

_PAGE_SIZE = 24


@router.get("/", response_class=HTMLResponse)
def feed(
    request: Request,
    min_score: float = 0.0,
    new_today: bool = False,
    hide_sold: bool = True,
    page: int = 0,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # Latest appraisal per listing, joined to the listing, sorted by deal_score.
    stmt = (
        select(Listing, Valuation)
        .join(Valuation, Valuation.listing_id == Listing.id)
        .where(Valuation.tier == ValuationTier.APPRAISE)
        .where(Valuation.deal_score >= min_score)
    )
    if hide_sold:
        stmt = stmt.where(Listing.status != ListingStatus.SOLD)
    if new_today:
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        stmt = stmt.where(Listing.first_seen_at >= cutoff)
    stmt = stmt.order_by(Valuation.deal_score.desc()).limit(_PAGE_SIZE).offset(
        page * _PAGE_SIZE
    )

    rows = db.execute(stmt).all()
    # Collapse to the most recent appraisal per listing (query may surface older ones).
    best: dict[str, tuple[Listing, Valuation]] = {}
    for listing, val in rows:
        prev = best.get(listing.id)
        if prev is None or (val.created_at or datetime.min) > (
            prev[1].created_at or datetime.min
        ):
            best[listing.id] = (listing, val)
    items = sorted(best.values(), key=lambda lv: lv[1].deal_score or 0, reverse=True)

    return templates.TemplateResponse(
        request,
        "feed.html",
        {
            "items": items,
            "min_score": min_score,
            "new_today": new_today,
            "hide_sold": hide_sold,
            "page": page,
            "has_next": len(rows) == _PAGE_SIZE,
        },
    )
