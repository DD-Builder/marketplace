"""The scrape -> triage -> appraise -> score -> persist pipeline.

This is the end-to-end seam the Phase-1 thin slice must prove. One call processes a
single search target and records a :class:`ScrapeRun` for observability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from dealfinder.config import get_settings
from dealfinder.core import repository
from dealfinder.core.enums import ScrapeRunStatus, ValuationTier
from dealfinder.core.models import ScrapeRun, SearchTarget, Valuation
from dealfinder.core.schemas import RawListing
from dealfinder.logging import get_logger
from dealfinder.scraper import photos, search
from dealfinder.scraper.errors import BlockedError, LayoutChangedError
from dealfinder.scraper.pacing import RateGovernor
from dealfinder.valuation import appraise as appraise_mod
from dealfinder.valuation import scoring, triage

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _process_listing(
    session: Session, raw: RawListing, target_id: int, settings
) -> bool:
    """Persist one listing and run the two-tier valuation. Returns True if appraised."""
    listing = repository.upsert_listing(session, raw, target_id=target_id)

    # Download photos and attach local paths to the freshly-created photo rows.
    downloaded = await photos.download_all([p.remote_url for p in raw.photos])
    for photo_row, (path, digest) in zip(listing.photos, downloaded):
        photo_row.local_path = str(path)
        photo_row.sha256 = digest
    session.flush()

    # Tier 1: cheap text triage.
    triage_result = triage.triage_listing(raw)
    session.add(
        Valuation(
            listing_id=listing.id,
            tier=ValuationTier.TRIAGE,
            model_id=settings.triage_model,
            reasoning=triage_result.reason,
            materials=triage_result.red_flags,
            identified_item=triage_result.rough_category,
        )
    )
    if not triage_result.promising:
        return False

    # Tier 2: Opus vision appraisal on promising items only.
    image_paths = [Path(p.local_path) for p in listing.photos if p.local_path]
    appraisal, in_tok, out_tok = appraise_mod.appraise(
        description=listing.description,
        asking_price_cents=listing.asking_price_cents,
        image_paths=image_paths,
    )
    computed = scoring.compute_deal_score(
        appraisal, listing.asking_price_cents, settings.hourly_rate_cents
    )
    repository.add_valuation(
        session,
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


async def run_target(session: Session, target: SearchTarget) -> ScrapeRun:
    """Run one full scrape cycle for a target, recording a ScrapeRun."""
    settings = get_settings()
    governor = RateGovernor(settings.rate_max_actions_per_hour)

    run = ScrapeRun(search_target_id=target.id, started_at=_utcnow())
    session.add(run)
    session.flush()

    try:
        ids = await search.enumerate_ids(target, governor)
        run.listings_found = len(ids)

        new_ids = repository.filter_new_ids(session, target.id, ids)
        run.new_listings = len(new_ids)
        repository.record_seen(session, target.id, ids)
        session.flush()

        appraised = 0
        for fb_id in new_ids:
            try:
                raw = await search.scrape_detail(fb_id, governor)
            except LayoutChangedError as exc:
                log.warning("layout_changed", listing=fb_id, error=str(exc))
                run.status = ScrapeRunStatus.LAYOUT_ERROR
                run.notes = f"layout error on {fb_id}: {exc}"
                continue
            except BlockedError:
                raise  # handled below — stop the whole run
            if await _process_listing(session, raw, target.id, settings):
                appraised += 1
            session.flush()

        run.appraised = appraised
        if run.status == ScrapeRunStatus.OK:
            run.notes = (
                f"found {run.listings_found}, {run.new_listings} new, "
                f"{appraised} appraised"
            )

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
        session.flush()

    return run
