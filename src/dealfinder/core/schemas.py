"""Pydantic DTOs used at layer boundaries (scraper output, valuation I/O, API)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RawPhoto(BaseModel):
    remote_url: str
    position: int = 0


class RawListing(BaseModel):
    """A listing as extracted by the scraper, before persistence."""

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


class TriageResult(BaseModel):
    """Tier-1 cheap-model filter output (structured)."""

    promising: bool
    rough_category: str = ""
    reason: str = ""
    red_flags: list[str] = Field(default_factory=list)


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
    deal_score: float = Field(ge=0.0, le=100.0)  # the model's own estimate
    reasoning: str = ""


class NegotiationDraft(BaseModel):
    text: str
    rationale: str = ""


class NegotiationDrafts(BaseModel):
    drafts: list[NegotiationDraft]
