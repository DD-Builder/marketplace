"""Negotiation drafting panel — human-in-the-loop, no auto-send."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dealfinder.core import repository
from dealfinder.core.db import get_db, session_scope
from dealfinder.core.enums import MessageRole
from dealfinder.core.models import Listing, NegotiationMessage, NegotiationThread
from dealfinder.negotiation.draft import draft_replies
from dealfinder.web.deps import templates

router = APIRouter()


def _get_or_create_thread(db: Session, listing_id: str) -> NegotiationThread:
    thread = db.execute(
        select(NegotiationThread).where(NegotiationThread.listing_id == listing_id)
    ).scalars().first()
    if thread is None:
        thread = NegotiationThread(listing_id=listing_id)
        db.add(thread)
        db.flush()
    return thread


@router.get("/listing/{listing_id}/negotiate", response_class=HTMLResponse)
def negotiate(
    listing_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="listing not found")
    thread = _get_or_create_thread(db, listing_id)
    db.commit()
    valuation = repository.current_valuation(db, listing_id)
    return templates.TemplateResponse(
        request,
        "negotiate.html",
        {
            "listing": listing,
            "thread": thread,
            "valuation": valuation,
            "drafts": None,
        },
    )


@router.post("/listing/{listing_id}/negotiate", response_class=HTMLResponse)
def make_drafts(
    listing_id: str,
    request: Request,
    seller_message: str = Form(""),
    posture: int = Form(50),
    target_price: str = Form(""),
) -> HTMLResponse:
    with session_scope() as db:
        listing = db.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="listing not found")
        thread = _get_or_create_thread(db, listing_id)
        thread.posture = posture
        if target_price.strip():
            try:
                thread.target_price_cents = int(round(float(target_price) * 100))
            except ValueError:
                pass

        if seller_message.strip():
            db.add(
                NegotiationMessage(
                    thread_id=thread.id,
                    role=MessageRole.SELLER,
                    content=seller_message.strip(),
                )
            )
            db.flush()

        # Build the conversation transcript from stored messages.
        transcript = "\n".join(
            f"{m.role.value}: {m.content}"
            for m in sorted(thread.messages, key=lambda m: m.created_at)
        )
        valuation = repository.current_valuation(db, listing_id)
        condition = valuation.condition_assessment if valuation else ""

        drafts = draft_replies(
            posture=posture,
            target_price_cents=thread.target_price_cents,
            asking_price_cents=listing.asking_price_cents,
            listing_title=listing.title,
            condition_notes=condition,
            conversation=transcript,
        )
        for d in drafts.drafts:
            db.add(
                NegotiationMessage(
                    thread_id=thread.id, role=MessageRole.AI_DRAFT, content=d.text
                )
            )

        # Re-read for the template within this transaction.
        context = {
            "listing": listing,
            "thread": thread,
            "valuation": valuation,
            "drafts": drafts.drafts,
        }
        return templates.TemplateResponse(request, "negotiate.html", context)


@router.post("/listing/{listing_id}/negotiate/sent")
def mark_sent(
    listing_id: str, text: str = Form(...)
) -> RedirectResponse:
    """Record that the user actually sent a message (copied into Messenger by hand)."""
    with session_scope() as db:
        thread = _get_or_create_thread(db, listing_id)
        db.add(
            NegotiationMessage(
                thread_id=thread.id,
                role=MessageRole.USER_SENT,
                content=text,
                chosen=True,
            )
        )
    return RedirectResponse(url=f"/listing/{listing_id}/negotiate", status_code=303)
