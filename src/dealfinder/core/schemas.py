"""Pydantic DTOs used at layer boundaries (scraper output, valuation I/O, API)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RawPhoto(BaseModel):
    remote_url: str
    position: int = 0


class RawListing(BaseModel):
    """A listing as extracted by the scraper, before persistence.

    A record may be *thin* (from a cheap search-grid scan) or *full* (from a paid detail
    page). Only ``description`` and the full photo set require the detail page; everything
    else below is free in the grid. ``detail_fetched`` records which kind this is, so the
    pipeline can avoid paying twice for the same listing.
    """

    fb_listing_id: str
    title: str = ""
    description: str = ""
    asking_price_cents: int | None = None
    currency: str = "USD"
    location_text: str = ""
    seller_name: str = ""
    seller_profile_url: str | None = None
    url: str = ""
    photos: list[RawPhoto] = Field(default_factory=list)
    raw_json: dict = Field(default_factory=dict)

    # Provenance and grid-level state (all free — no detail-page fetch required).
    detail_fetched: bool = False
    is_sold: bool | None = None
    is_live: bool | None = None
    posted_at: datetime | None = None
    #: When this record was actually observed on Marketplace. A live scrape leaves it
    #: None (meaning "now"); a dataset recovered from an old run sets it to that run's
    #: time, so three-day-old evidence cannot masquerade as a fresh sighting.
    observed_at: datetime | None = None


class CompRecord(BaseModel):
    """One comparable sale the appraiser located. Deliberately raw evidence, not a view."""

    price_cents: int
    title: str = ""
    medium: str = ""
    width_in: float = 0.0
    height_in: float = 0.0
    year_sold: int = 0
    venue: str = ""
    url: str = ""
    #: False for an asking price. The distinction is the whole point: for a living
    #: decorative artist the ask and the hammer differ several-fold, and the ask is the
    #: one that loses money.
    is_sold: bool = True


class AppraisalResult(BaseModel):
    """Tier-2 Opus vision appraisal output (structured via messages.parse)."""

    identified_item: str
    style_era: str = ""
    maker_guess: str | None = None
    materials: list[str] = Field(default_factory=list)
    condition_assessment: str = ""
    est_asis_value_cents: int
    est_restored_resale_value_cents: int
    est_restoration_cost_cents: int
    est_restoration_effort_hours: float
    confidence: float = Field(ge=0.0, le=1.0)
    #: The model's own estimate, and purely advisory — :func:`compute_deal_score` is the
    #: authoritative one and never reads this. It carries a default because it was the
    #: single required field the model sometimes omits, and without one a whole appraisal
    #: with every value field correctly populated was thrown away over it. Valuation is
    #: the only metered step in the pipeline; discarding a good one on an unused field is
    #: the most expensive possible way to be strict.
    deal_score: float = Field(default=0.0, ge=0.0, le=100.0)
    reasoning: str = ""
    #: Realised sales the appraiser found, as records rather than as a conclusion. The
    #: arithmetic over these is done in :mod:`dealfinder.valuation.artcomps`, in code that
    #: can be inspected, precisely because a model's point estimate cannot be audited —
    #: the $1,200 Nino Pippa valuation was unfalsifiable until someone went and looked up
    #: the artist. Empty is normal and honest for an unlisted maker.
    comps: list[CompRecord] = Field(default_factory=list)


class NegotiationDraft(BaseModel):
    text: str
    rationale: str = ""


class NegotiationDrafts(BaseModel):
    drafts: list[NegotiationDraft]
