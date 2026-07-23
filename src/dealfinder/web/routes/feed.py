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
    # Exactly one row per listing — its current appraisal — so pagination is exact and
    # a superseded valuation never ranks (finding B3).
    stmt = (
        select(Listing, Valuation)
        .join(Valuation, Valuation.listing_id == Listing.id)
        .where(Valuation.tier == ValuationTier.APPRAISE)
        .where(Valuation.is_current.is_(True))
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

    items = db.execute(stmt).all()

    return templates.TemplateResponse(
        request,
        "feed.html",
        {
            "items": items,
            "min_score": min_score,
            "new_today": new_today,
            "hide_sold": hide_sold,
            "page": page,
            "has_next": len(items) == _PAGE_SIZE,
        },
    )
