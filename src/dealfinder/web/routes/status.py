"""Scrape-run history and worker health."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dealfinder.config import get_settings
from dealfinder.core.db import get_db
from dealfinder.core.models import Listing, ScrapeRun, SearchTarget, Valuation
from dealfinder.web.deps import templates

router = APIRouter()


@router.get("/status", response_class=HTMLResponse)
def status(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    runs = db.execute(
        select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(30)
    ).scalars().all()
    target_names = {
        t.id: t.name for t in db.execute(select(SearchTarget)).scalars().all()
    }
    counts = {
        "listings": db.execute(select(func.count(Listing.id))).scalar_one(),
        "valuations": db.execute(select(func.count(Valuation.id))).scalar_one(),
        "targets": db.execute(select(func.count(SearchTarget.id))).scalar_one(),
    }
    settings = get_settings()
    burner = "configured" if settings.fb_session_path else "not configured (logged-out only)"
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "runs": runs,
            "target_names": target_names,
            "counts": counts,
            "burner": burner,
        },
    )
