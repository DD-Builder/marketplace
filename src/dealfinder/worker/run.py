"""``dealfinder-worker`` entrypoint.

  dealfinder-worker --once            run every enabled target once, then exit
  dealfinder-worker --once --target N run a single target once, then exit
  dealfinder-worker                   start the always-on scheduler loop
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from dealfinder.core.db import init_db, session_scope
from dealfinder.core.models import SearchTarget
from dealfinder.logging import configure_logging, get_logger
from dealfinder.worker.pipeline import run_target
from dealfinder.worker.scheduler import build_scheduler

log = get_logger(__name__)


async def _run_once(target_id: int | None) -> None:
    with session_scope() as session:
        stmt = select(SearchTarget).where(SearchTarget.enabled.is_(True))
        if target_id is not None:
            stmt = select(SearchTarget).where(SearchTarget.id == target_id)
        targets = session.execute(stmt).scalars().all()
        if not targets:
            log.warning("no_targets", hint="seed one via the dashboard /targets first")
            return
        for target in targets:
            log.info("run_once_start", target=target.name)
            run = await run_target(session, target)
            log.info(
                "run_once_done",
                target=target.name,
                status=run.status,
                found=run.listings_found,
                new=run.new_listings,
                appraised=run.appraised,
                notes=run.notes,
            )


async def _serve() -> None:
    scheduler = build_scheduler()
    scheduler.start()
    log.info("worker_started", mode="scheduler")
    stop = asyncio.Event()
    try:
        await stop.wait()  # run until cancelled / SIGINT
    finally:
        scheduler.shutdown(wait=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deal-finder scraper worker")
    parser.add_argument("--once", action="store_true", help="run once and exit")
    parser.add_argument("--target", type=int, default=None, help="limit to one target id")
    args = parser.parse_args()

    configure_logging()
    init_db()

    if args.once:
        asyncio.run(_run_once(args.target))
    else:
        try:
            asyncio.run(_serve())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
