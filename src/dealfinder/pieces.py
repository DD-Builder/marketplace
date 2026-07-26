"""Your books: what you actually paid, what you put into it, and what it sold for.

This is the half of the app that isn't about finding furniture. Every piece you buy gets an
entry — price paid, materials, hands-on hours, and eventually a sale price — and those
entries feed two things:

* the **second resale tier**, so a piece on the board shows what it's worth *to you* rather
  than only what it's worth;
* your **realised history**, which is the only honest answer to "is this actually worth my
  Saturdays" — cash profit, profit after valuing your time, and effective hourly wage.

Stored as one JSON file next to the site, for the same reason the catalogue is: the job runs
in a stateless Action, so the repo is the durable storage.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from dealfinder.logging import get_logger
from dealfinder.resale import PieceCosts, RealizedOutcome, realized

log = get_logger(__name__)

LEDGER_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PieceLog(BaseModel):
    """One piece you actually bought."""

    listing_id: str
    title: str = ""
    acquired_price_cents: int = 0
    materials_cents: int = 0
    labor_hours: float = 0.0
    acquired_at: datetime | None = None
    sold_price_cents: int | None = None
    sold_at: datetime | None = None
    notes: str = ""

    @property
    def costs(self) -> PieceCosts:
        return PieceCosts(
            acquisition_cents=self.acquired_price_cents,
            materials_cents=self.materials_cents,
            labor_hours=self.labor_hours,
        )

    @property
    def is_sold(self) -> bool:
        return self.sold_price_cents is not None


class Ledger(BaseModel):
    version: int = LEDGER_VERSION
    updated_at: datetime = Field(default_factory=_now)
    pieces: dict[str, PieceLog] = Field(default_factory=dict)


def load_ledger(path: Path) -> Ledger:
    """Load your books, degrading to empty rather than killing a run."""
    path = Path(path)
    if not path.exists():
        return Ledger()
    try:
        return Ledger.model_validate_json(path.read_text())
    except (ValidationError, ValueError) as exc:
        log.warning("ledger_unreadable", path=str(path), error=str(exc)[:200])
        return Ledger()


def save_ledger(ledger: Ledger, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger.updated_at = _now()
    payload = ledger.model_dump(mode="json")
    payload["pieces"] = dict(sorted(payload.get("pieces", {}).items()))
    path.write_text(json.dumps(payload, indent=1, sort_keys=True))


def costs_by_id(ledger: Ledger) -> dict[str, PieceCosts]:
    """What :func:`dealfinder.engine.evaluate_piece` needs to personalise a card.

    Only pieces with something actually entered — an empty log would silently replace the
    honest "bought at ask, restored per estimate" estimate with a basis of zero, which would
    make every piece look free.
    """
    return {
        pid: log_.costs
        for pid, log_ in ledger.pieces.items()
        if log_.acquired_price_cents or log_.materials_cents or log_.labor_hours
    }


class HistorySummary(BaseModel):
    """Your realised record across everything sold. The answer to 'is this worth it?'"""

    sold_count: int = 0
    cash_invested_cents: int = 0
    revenue_cents: int = 0
    cash_profit_cents: int = 0
    net_profit_cents: int = 0          # after valuing your own hours
    hours: float = 0.0
    effective_hourly_cents: int | None = None
    best_piece: str = ""
    worst_piece: str = ""

    @property
    def has_data(self) -> bool:
        return self.sold_count > 0


def outcomes(ledger: Ledger, hourly_rate_cents: int) -> dict[str, RealizedOutcome]:
    return {
        pid: realized(p.sold_price_cents or 0, p.costs, hourly_rate_cents)
        for pid, p in ledger.pieces.items()
        if p.is_sold
    }


def history(ledger: Ledger, hourly_rate_cents: int) -> HistorySummary:
    sold = [p for p in ledger.pieces.values() if p.is_sold]
    if not sold:
        return HistorySummary()

    per_piece = outcomes(ledger, hourly_rate_cents)
    hours = sum(p.labor_hours for p in sold)
    cash_profit = sum(o.cash_profit_cents for o in per_piece.values())
    ranked = sorted(sold, key=lambda p: per_piece[p.listing_id].cash_profit_cents)

    return HistorySummary(
        sold_count=len(sold),
        cash_invested_cents=sum(p.costs.acquisition_cents + p.materials_cents for p in sold),
        revenue_cents=sum(p.sold_price_cents or 0 for p in sold),
        cash_profit_cents=cash_profit,
        net_profit_cents=sum(o.net_profit_cents for o in per_piece.values()),
        hours=round(hours, 1),
        effective_hourly_cents=round(cash_profit / hours) if hours > 0 else None,
        best_piece=ranked[-1].title or ranked[-1].listing_id,
        worst_piece=ranked[0].title or ranked[0].listing_id,
    )


def upsert(ledger: Ledger, entry: PieceLog) -> PieceLog:
    """Add or update one piece, preserving fields the caller didn't set."""
    existing = ledger.pieces.get(entry.listing_id)
    if existing is None:
        entry.acquired_at = entry.acquired_at or _now()
        ledger.pieces[entry.listing_id] = entry
        return entry
    merged = existing.model_copy(
        update={k: v for k, v in entry.model_dump(exclude_unset=True).items() if v is not None}
    )
    if merged.is_sold and merged.sold_at is None:
        merged = merged.model_copy(update={"sold_at": _now()})
    ledger.pieces[entry.listing_id] = merged
    return merged


def titles_from(pieces: Iterable) -> dict[str, str]:
    """Listing id -> title, so a logged piece keeps a readable name after it delists."""
    return {p.listing.fb_listing_id: p.listing.title for p in pieces}
