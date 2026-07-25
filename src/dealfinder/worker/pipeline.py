"""The scrape -> triage -> appraise -> score -> persist pipeline.

This is the end-to-end seam the Phase-1 thin slice must prove. One call processes a
single search target and records a :class:`ScrapeRun` for observability. A single
:class:`BrowserSession` is reused for the whole cycle and passed in (so tests can inject
a fake and the browser is opened once, not per page).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from dealfinder.config import get_settings
from dealfinder.core import repository
from dealfinder.core.enums import ScrapeRunStatus, ValuationTier
from dealfinder.core.models import Listing, ScrapeRun, SearchTarget, Valuation
from dealfinder.core.schemas import RawListing
from dealfinder.logging import get_logger
from dealfinder.scraper import photos, search
from dealfinder.scraper.errors import BlockedError, LayoutChangedError
from dealfinder.scraper.pacing import get_governor
from dealfinder.scraper.session import BrowserSession
from dealfinder.valuation import appraise as appraise_mod
from dealfinder.valuation import scoring, triage

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _download_missing_photos(
    session: BrowserSession, listing: Listing
) -> None:
    """Fetch only photos that don't yet have a local file, assigning per-row so a failed
    download can't shift paths onto the wrong row (findings B6/B7)."""
    for photo_row in listing.photos:
        if photo_row.local_path:
            continue
        stored = await photos.fetch_and_store(session, photo_row.remote_url)
        if stored is not None:
            path, digest = stored
            photo_row.local_path = str(path)
            photo_row.sha256 = digest


async def _process_listing(
    db: Session,
    session: BrowserSession,
    raw: RawListing,
    target_id: int,
    settings,
    *,
    may_appraise: bool,
) -> bool:
    """Persist one listing and run the two-tier valuation. Returns True if appraised."""
    listing = repository.upsert_listing(db, raw, target_id=target_id)
    await _download_missing_photos(session, listing)
    db.flush()

    # Tier 1: cheap text triage (blocking HTTP -> off the event loop).
    triage_result = await asyncio.to_thread(triage.triage_listing, raw)
    flags = ("; ".join(triage_result.red_flags)) if triage_result.red_flags else ""
    db.add(
        Valuation(
            listing_id=listing.id,
            tier=ValuationTier.TRIAGE,
            model_id=settings.triage_model,
            identified_item=triage_result.rough_category,
            reasoning=(triage_result.reason + (f" | red flags: {flags}" if flags else "")),
        )
    )
    if not triage_result.promising or not may_appraise:
        return False

    # Tier 2: Opus vision appraisal on promising items only.
    image_paths = [Path(p.local_path) for p in listing.photos if p.local_path]
    appraisal, in_tok, out_tok = await asyncio.to_thread(
        lambda: appraise_mod.appraise(
            description=listing.description,
            asking_price_cents=listing.asking_price_cents,
            image_paths=image_paths,
        )
    )
    computed = scoring.compute_deal_score(
        appraisal, listing.asking_price_cents, settings.hourly_rate_cents
    )
    repository.add_appraisal(
        db,
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
        ),
    )
    return True


async def run_target(
    db: Session, target: SearchTarget, browser: BrowserSession | None = None
) -> ScrapeRun:
    """Run one full scrape cycle for a target, recording a ScrapeRun.

    ``browser`` may be injected (tests, or a shared session); otherwise one is opened for
    this cycle and closed at the end.
    """
    settings = get_settings()
    governor = get_governor()

    own_browser = browser is None
    if own_browser:
        browser = await BrowserSession().start()

    run = ScrapeRun(search_target_id=target.id, started_at=_utcnow())
    db.add(run)
    db.flush()

    try:
        ids = await search.enumerate_ids(target, browser, governor)
        run.listings_found = len(ids)

        new_ids = repository.filter_new_ids(db, target.id, ids)
        run.new_listings = len(new_ids)
        repository.record_seen(db, target.id, ids)
        db.flush()

        appraised = 0
        capped = False
        for fb_id in new_ids:
            try:
                raw = await search.scrape_detail(fb_id, browser, governor)
            except LayoutChangedError as exc:
                log.warning("layout_changed", listing=fb_id, error=str(exc))
                run.status = ScrapeRunStatus.LAYOUT_ERROR
                run.notes = f"layout error on {fb_id}: {exc}"
                continue
            except BlockedError:
                raise  # handled below — stop the whole run
            may_appraise = appraised < settings.max_appraisals_per_run
            if not may_appraise:
                capped = True
            if await _process_listing(
                db, browser, raw, target.id, settings, may_appraise=may_appraise
            ):
                appraised += 1
            db.flush()

        run.appraised = appraised
        if run.status == ScrapeRunStatus.OK:
            note = (
                f"found {run.listings_found}, {run.new_listings} new, "
                f"{appraised} appraised"
            )
            if capped:
                note += f" (appraisal cap {settings.max_appraisals_per_run} hit)"
            run.notes = note

    except BlockedError as exc:
        run.status = ScrapeRunStatus.BLOCKED
        run.notes = f"blocked (checkpoint={exc.checkpoint}): {exc}"
        log.warning("scrape_blocked", target=target.name, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        run.status = ScrapeRunStatus.ERROR
        run.notes = f"error: {exc}"
        log.error("scrape_error", target=target.name, error=str(exc))
    finally:
        run.finished_at = _utcnow()
        db.flush()
        if own_browser:
            await browser.close()

    return run
