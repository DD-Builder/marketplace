"""End-to-end pipeline test with the scraper + LLM boundaries mocked.

Proves the Phase-1 seam: enumerate -> dedup -> scrape -> triage -> appraise -> score ->
persist, and that the result reaches the deal feed. No network, browser, or API calls.
"""

from __future__ import annotations

import asyncio

from dealfinder.core.enums import ValuationTier
from dealfinder.core.models import SearchTarget, Valuation
from dealfinder.core.schemas import AppraisalResult, RawListing, TriageResult
from dealfinder.scraper import parse
from dealfinder.worker import pipeline
from tests.conftest import load_fixture


def _fake_raw() -> RawListing:
    return parse.parse_listing_detail(load_fixture("listing_detail.html"))


class _FakeBrowser:
    """Stands in for BrowserSession — no real Chrome in tests."""

    async def fetch_bytes(self, url):
        return None


def test_run_target_populates_feed(temp_db, monkeypatch):
    raw = _fake_raw()

    async def fake_enumerate(target, session, governor):
        return [raw.fb_listing_id]

    async def fake_scrape(fb_id, session, governor):
        return raw

    async def fake_fetch_and_store(session, url):
        return None  # skip real photo downloads

    def fake_triage(listing):
        return TriageResult(promising=True, rough_category="sideboard")

    def fake_appraise(*, description, asking_price_cents, image_paths, comps=None):
        result = AppraisalResult(
            identified_item="Danish teak sideboard",
            style_era="Mid-century",
            maker_guess=None,
            materials=["teak"],
            condition_assessment="Scratched top, sticky drawer.",
            est_asis_value_cents=15000,
            est_restored_resale_value_cents=60000,
            est_restoration_cost_cents=5000,
            est_restoration_effort_hours=4.0,
            confidence=0.8,
            deal_score=70.0,
            reasoning="Underpriced for the maker.",
        )
        return result, 100, 50

    monkeypatch.setattr(pipeline.search, "enumerate_ids", fake_enumerate)
    monkeypatch.setattr(pipeline.search, "scrape_detail", fake_scrape)
    monkeypatch.setattr(pipeline.photos, "fetch_and_store", fake_fetch_and_store)
    monkeypatch.setattr(pipeline.triage, "triage_listing", fake_triage)
    monkeypatch.setattr(pipeline.appraise_mod, "appraise", fake_appraise)

    browser = _FakeBrowser()
    with temp_db.session_scope() as session:
        target = SearchTarget(name="test", category="furniture", location="nyc")
        session.add(target)
        session.flush()
        run = asyncio.run(pipeline.run_target(session, target, browser=browser))
        assert run.new_listings == 1
        assert run.appraised == 1

    # The appraisal landed with a positive computed deal score.
    with temp_db.session_scope() as session:
        val = session.query(Valuation).filter(
            Valuation.tier == ValuationTier.APPRAISE
        ).one()
        assert val.identified_item == "Danish teak sideboard"
        assert val.deal_score and val.deal_score > 0

    # A second run dedups — no new listings, no new appraisal.
    with temp_db.session_scope() as session:
        target = session.query(SearchTarget).one()
        run2 = asyncio.run(pipeline.run_target(session, target, browser=browser))
        assert run2.new_listings == 0
