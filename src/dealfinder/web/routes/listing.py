"""Listing detail view + re-appraise action."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dealfinder.core import repository
from dealfinder.core.db import get_db, session_scope
from dealfinder.core.enums import ValuationTier
from dealfinder.core.models import Listing, PriceHistory, Valuation
from dealfinder.config import get_settings
from dealfinder.valuation import appraise as appraise_mod
from dealfinder.valuation import scoring
from dealfinder.web.deps import templates

router = APIRouter()


@router.get("/listing/{listing_id}", response_class=HTMLResponse)
def listing_detail(
    listing_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="listing not found")

    valuation = repository.current_valuation(db, listing_id)
    history = db.execute(
        select(PriceHistory)
        .where(PriceHistory.listing_id == listing_id)
        .order_by(PriceHistory.observed_at)
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "listing.html",
        {"listing": listing, "valuation": valuation, "history": history},
    )


@router.post("/listing/{listing_id}/reappraise")
def reappraise(listing_id: str) -> RedirectResponse:
    """Re-run the Tier-2 appraisal for a listing using its stored photos."""
    settings = get_settings()
    with session_scope() as db:
        listing = db.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="listing not found")
        image_paths = [Path(p.local_path) for p in listing.photos if p.local_path]
        appraisal, in_tok, out_tok = appraise_mod.appraise(
            description=listing.description,
            asking_price_cents=listing.asking_price_cents,
            image_paths=image_paths,
        )
        computed = scoring.compute_deal_score(
            appraisal, listing.asking_price_cents, settings.hourly_rate_cents
        )
        db.add(
            Valuation(
                listing_id=listing.id,
                tier=ValuationTier.APPRAISE,
                model_id=settings.appraise_model,
                identified_item=appraisal.identified_item,
                style_era=appraisal.style_era,
                maker_guess=appraisal.maker_guess,
                materials=appraisal.materials,
                condition_assessment=appraisal.condition_assessment,
                est_asis_value_cents=appraisal.est_asis_value_cents,
                est_restored_resale_value_cents=appraisal.est_restored_resale_value_cents,
                est_restoration_cost_cents=appraisal.est_restoration_cost_cents,
                est_restoration_effort_hours=appraisal.est_restoration_effort_hours,
                confidence=appraisal.confidence,
                model_deal_score=appraisal.deal_score,
                deal_score=computed,
                reasoning=appraisal.reasoning,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
        )
    return RedirectResponse(url=f"/listing/{listing_id}", status_code=303)
