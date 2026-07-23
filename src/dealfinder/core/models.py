"""SQLAlchemy 2.x ORM models.

Design notes:
- Money is stored as integer *cents* everywhere.
- AI outputs and lists are stored as JSON columns (native JSONB on Postgres later).
- ``listings.id`` uses Facebook's own listing ID as the natural dedup key.
- The same models run on SQLite (MVP) and Postgres by changing ``DATABASE_URL``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import enum as _enum
from typing import TypeVar

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from dealfinder.core.enums import (
    ListingStatus,
    MessageRole,
    ScrapeRunStatus,
    ValuationTier,
)

_E = TypeVar("_E", bound=_enum.Enum)


def enum_col(enum_cls: type[_E]):
    """A string-backed Enum column that stores the member *value* and reads it back
    as the enum member (so ``.value`` works on read — see audit finding B2)."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=16,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SearchTarget(Base):
    """Defines what to enumerate: a category x location x keyword x price window."""

    __tablename__ = "search_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(120))
    location: Mapped[str] = mapped_column(String(120))       # FB place slug or "lat,lon"
    radius_km: Mapped[int] = mapped_column(Integer, default=40)
    query: Mapped[str | None] = mapped_column(String(200), nullable=True)
    min_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    focused: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Listing(Base):
    """One row per unique marketplace item."""

    __tablename__ = "listings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # FB listing id
    search_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_targets.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    asking_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    location_text: Mapped[str] = mapped_column(String(200), default="")
    seller_name: Mapped[str] = mapped_column(String(200), default="")
    seller_profile_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[ListingStatus] = mapped_column(
        enum_col(ListingStatus), default=ListingStatus.NEW
    )
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    photos: Mapped[list["ListingPhoto"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )
    valuations: Mapped[list["Valuation"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )
    price_history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class ListingPhoto(Base):
    __tablename__ = "listing_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id"))
    remote_url: Mapped[str] = mapped_column(String(1000), default="")
    local_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    listing: Mapped[Listing] = relationship(back_populates="photos")


class PriceHistory(Base):
    """Captures price changes / relists over time. A price drop is itself a buy signal."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id"))
    price_cents: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    listing: Mapped[Listing] = relationship(back_populates="price_history")


class Valuation(Base):
    """One row per AI appraisal run (history kept; models/prompts evolve)."""

    __tablename__ = "valuations"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id"))
    tier: Mapped[ValuationTier] = mapped_column(enum_col(ValuationTier))
    model_id: Mapped[str] = mapped_column(String(64), default="")

    identified_item: Mapped[str] = mapped_column(String(300), default="")
    style_era: Mapped[str] = mapped_column(String(200), default="")
    maker_guess: Mapped[str | None] = mapped_column(String(200), nullable=True)
    materials: Mapped[list] = mapped_column(JSON, default=list)
    condition_assessment: Mapped[str] = mapped_column(Text, default="")

    est_asis_value_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    est_restored_resale_value_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    est_restoration_cost_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    est_restoration_effort_hours: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    model_deal_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    deal_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # computed
    # True for the single most-recent appraisal per listing; the feed ranks on this so
    # a re-appraisal supersedes the old score instead of both competing (finding B3).
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    reasoning: Mapped[str] = mapped_column(Text, default="")

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    listing: Mapped[Listing] = relationship(back_populates="valuations")


class SeenSet(Base):
    """Per-target enumeration ledger for cheap 'what's new since last cycle' diffs."""

    __tablename__ = "seen_set"
    __table_args__ = (
        UniqueConstraint("search_target_id", "fb_listing_id", name="uq_seen_target_item"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    search_target_id: Mapped[int] = mapped_column(ForeignKey("search_targets.id"))
    fb_listing_id: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class NegotiationThread(Base):
    __tablename__ = "negotiation_threads"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id"))
    posture: Mapped[int] = mapped_column(Integer, default=50)  # 0=aggressive..100=eager
    target_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    messages: Mapped[list["NegotiationMessage"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )


class NegotiationMessage(Base):
    __tablename__ = "negotiation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("negotiation_threads.id"))
    role: Mapped[MessageRole] = mapped_column(enum_col(MessageRole))
    content: Mapped[str] = mapped_column(Text, default="")
    # A future auto-send flips a chosen ai_draft to user_sent and dispatches it.
    chosen: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    thread: Mapped[NegotiationThread] = relationship(back_populates="messages")


class ScrapeRun(Base):
    """Observability for the always-on loop."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    search_target_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_targets.id"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    listings_found: Mapped[int] = mapped_column(Integer, default=0)
    new_listings: Mapped[int] = mapped_column(Integer, default=0)
    appraised: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[ScrapeRunStatus] = mapped_column(
        enum_col(ScrapeRunStatus), default=ScrapeRunStatus.OK
    )
    notes: Mapped[str] = mapped_column(Text, default="")
