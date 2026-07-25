"""APScheduler-based always-on loop.

One interval job per enabled target, each with randomized base interval + jitter so
runs don't align to a detectable cadence. ``max_instances=1`` + ``coalesce`` prevent a
slow run from stacking. Jobs persist in the DB so they survive a home-machine reboot.
"""

from __future__ import annotations

import random

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from dealfinder.config import get_settings
from dealfinder.core.db import get_engine, session_scope
from dealfinder.core.models import SearchTarget
from dealfinder.logging import get_logger
from dealfinder.scraper.pacing import in_quiet_hours
from dealfinder.worker.pipeline import run_target

log = get_logger(__name__)


async def _run_target_job(target_id: int) -> None:
    if in_quiet_hours():
        log.info("quiet_hours_skip", target_id=target_id)
        return
    with session_scope() as session:
        target = session.get(SearchTarget, target_id)
        if target is None or not target.enabled:
            return
        await run_target(session, target)


def build_scheduler() -> AsyncIOScheduler:
    jobstore = SQLAlchemyJobStore(engine=get_engine())
    scheduler = AsyncIOScheduler(jobstores={"default": jobstore})

    with session_scope() as session:
        targets = session.execute(
            select(SearchTarget).where(SearchTarget.enabled.is_(True))
        ).scalars().all()
        for target in targets:
            # Focused targets poll more often than broad ones.
            base_minutes = random.randint(20, 40) if target.focused else random.randint(120, 240)
            scheduler.add_job(
                _run_target_job,
                trigger="interval",
                minutes=base_minutes,
                jitter=base_minutes * 6,  # up to ~10% jitter in seconds
                args=[target.id],
                id=f"target-{target.id}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            log.info("scheduled_target", target=target.name, minutes=base_minutes)
    return scheduler
