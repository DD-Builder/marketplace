"""Search-target CRUD and on-demand 'scrape now'."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dealfinder.core.db import get_db, session_scope
from dealfinder.core.models import SearchTarget
from dealfinder.web.deps import templates
from dealfinder.worker.pipeline import run_target

router = APIRouter()


def _dollars_to_cents(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    try:
        return int(round(float(value) * 100))
    except ValueError:
        return None


@router.get("/targets", response_class=HTMLResponse)
def list_targets(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    targets = db.execute(
        select(SearchTarget).order_by(SearchTarget.created_at.desc())
    ).scalars().all()
    return templates.TemplateResponse(request, "targets.html", {"targets": targets})


@router.post("/targets")
def create_target(
    name: str = Form(...),
    category: str = Form(""),
    location: str = Form(...),
    radius_km: int = Form(40),
    query: str = Form(""),
    min_price: str = Form(""),
    max_price: str = Form(""),
    focused: bool = Form(False),
) -> RedirectResponse:
    with session_scope() as db:
        db.add(
            SearchTarget(
                name=name,
                category=category,
                location=location,
                radius_km=radius_km,
                query=query or None,
                min_price_cents=_dollars_to_cents(min_price),
                max_price_cents=_dollars_to_cents(max_price),
                focused=focused,
                enabled=True,
            )
        )
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/{target_id}/toggle")
def toggle_target(target_id: int) -> RedirectResponse:
    with session_scope() as db:
        target = db.get(SearchTarget, target_id)
        if target is not None:
            target.enabled = not target.enabled
    return RedirectResponse(url="/targets", status_code=303)


@router.post("/targets/{target_id}/scrape")
def scrape_now(target_id: int) -> RedirectResponse:
    """Run one scrape cycle for this target immediately (blocking)."""
    with session_scope() as db:
        target = db.get(SearchTarget, target_id)
        if target is not None:
            asyncio.run(run_target(db, target))
    return RedirectResponse(url="/status", status_code=303)
