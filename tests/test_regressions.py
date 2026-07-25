"""Regression tests for audit findings B1, B2, B3."""

from __future__ import annotations

from datetime import datetime, timezone

from dealfinder.core.enums import (
    ListingStatus,
    MessageRole,
    ScrapeRunStatus,
    ValuationTier,
)
from dealfinder.core.models import Listing, ScrapeRun, Valuation


def test_b1_blank_quiet_hours_env_does_not_crash(monkeypatch):
    """The value shipped in .env.example must not break settings load."""
    from dealfinder import config

    monkeypatch.setenv("QUIET_HOURS_START", "")  # exactly as in .env.example
    monkeypatch.setenv("QUIET_HOURS_END", "")
    config.get_settings.cache_clear()
    settings = config.Settings()
    assert settings.quiet_hours_start is None
    assert settings.quiet_hours_end is None
    config.get_settings.cache_clear()


def test_b2_enum_columns_roundtrip_as_enum_members(temp_db):
    """Enum columns must read back as enum members so ``.value`` works (not bare str)."""
    with temp_db.session_scope() as s:
        s.add(ScrapeRun(status=ScrapeRunStatus.OK, started_at=datetime.now(timezone.utc)))
        s.add(Listing(id="1", status=ListingStatus.SOLD))
    with temp_db.session_scope() as s:  # fresh session = simulates a later request
        run = s.query(ScrapeRun).one()
        assert isinstance(run.status, ScrapeRunStatus)
        assert run.status.value == "ok"  # the template does exactly this
        listing = s.query(Listing).one()
        assert isinstance(listing.status, ListingStatus)


def test_b3_only_current_appraisal_is_marked(temp_db):
    """A re-appraisal supersedes the old one; only one is_current per listing."""
    from dealfinder.core import repository

    with temp_db.session_scope() as s:
        s.add(Listing(id="1", asking_price_cents=10000, status=ListingStatus.NEW))
        s.flush()
        repository.add_appraisal(
            s, Valuation(listing_id="1", tier=ValuationTier.APPRAISE, deal_score=90)
        )
        repository.add_appraisal(
            s, Valuation(listing_id="1", tier=ValuationTier.APPRAISE, deal_score=10)
        )

    with temp_db.session_scope() as s:
        current = repository.current_valuation(s, "1")
        assert current is not None and current.deal_score == 10  # the newer one wins
        n_current = (
            s.query(Valuation)
            .filter(Valuation.is_current.is_(True))
            .count()
        )
        assert n_current == 1


def test_message_role_roundtrips(temp_db):
    from dealfinder.core.models import NegotiationMessage, NegotiationThread

    with temp_db.session_scope() as s:
        s.add(Listing(id="1", status=ListingStatus.NEW))
        s.flush()
        th = NegotiationThread(listing_id="1")
        s.add(th)
        s.flush()
        s.add(NegotiationMessage(thread_id=th.id, role=MessageRole.SELLER, content="hi"))
    with temp_db.session_scope() as s:
        m = s.query(NegotiationMessage).one()
        assert isinstance(m.role, MessageRole)
        assert m.role.value == "seller"  # transcript building does this
