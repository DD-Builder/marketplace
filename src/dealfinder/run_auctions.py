"""One-command auction run: discover lots → snapshot bids → appraise → advise → publish.

The cadence trick: this runs *hourly* (auctions are won and lost inside a day), but an
hourly job must not cost hourly money. So each run is asymmetric —

* **snapshots** (cheap page fetches) happen every run, endgame lots first;
* **discovery** (search-page trawls for new lots) runs only when its interval is due;
* **appraisals** (the only expensive step) happen once per lot, capped per run.

Environment, mirroring run_board:

    CLAUDE_CODE_OAUTH_TOKEN   subscription appraiser auth (CI)
    APPRAISER_PROVIDER        claude-code (default) | claude-api
    EBTH_SEARCH_URLS          newline/comma-separated EBTH search/browse URLs
    EBTH_PREMIUM_PCT          buyer's premium assumption (default 0.15)
    EBTH_SHIPPING_CENTS       per-lot freight assumption (default 0 = local pickup)
    MAX_AUCTION_APPRAISALS    AI-call cap per run (default 6)
    EBTH_DISCOVERY_HOURS      how often to trawl for new lots (default 6)
    EBTH_SNAPSHOT_CAP         item-page fetches per run (default 40)
    EBTH_MAX_WATCH            watchlist size cap (default 40)

State lives in ``docs/auctions/catalog.json``; the page in ``docs/auctions/index.html``.
``--probe`` skips the pipeline and instead publishes a structure report of what
ebth.com actually serves — the development environment cannot reach the site, so the
probe run in CI is how the parsers get tightened from evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from dealfinder.auctions import bidding
from dealfinder.auctions import catalog as acat
from dealfinder.auctions.board import AuctionBoardMeta, write_auction_page
from dealfinder.appraiser import get_appraiser
from dealfinder.logging import get_logger
from dealfinder.prescreen import prescreen
from dealfinder.run_board import (
    _check_credentials,
    _download_photos,
    _env,
    _int_env,
    _search_urls,
    failure_reason,
)
from dealfinder.sources.ebth import EbthClient, build_client
from dealfinder.verticals import get_vertical

log = get_logger(__name__)

_DEFAULT_SEARCHES = (
    "https://www.ebth.com/search?q=mid%20century%20furniture\n"
    "https://www.ebth.com/search?q=danish%20modern"
)


def _write_status(out_dir: Path, state: str, **counts) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "status.json").write_text(
            json.dumps({"state": state,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        **counts}, indent=1),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("status_write_failed", error=str(exc)[:120])


def _probe(client: EbthClient, urls: list[str], out_dir: Path) -> int:
    report = client.probe(urls)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "probe.json"
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    for page in report["pages"]:
        line = f"{page['kind']}: {page['url']}"
        if "error" in page:
            print(f"{line}\n  FAILED: {page['error']}")
        else:
            cov = page.get("field_coverage", {})
            print(
                f"{line}\n  {page.get('harvested_items', 0)} items via "
                f"{page.get('parsed_by') or 'nothing'} · "
                f"bid coverage {cov.get('current_bid_cents', 0)} · "
                f"end-time coverage {cov.get('ends_at', 0)} · "
                f"links {page.get('item_links', 0)}"
            )
    print(f"wrote {path}")
    ok = any("error" not in p and p.get("harvested_items", 0) for p in report["pages"])
    return 0 if ok else 5


def _refresh_watchlist(catalog: acat.AuctionCatalog, vertical, cap: int) -> int:
    """Promote quality lots to the watchlist, newest evidence first.

    The gate is a *positive* signal — maker or material keywords — not merely "has a
    photo and a price" like the Marketplace pre-screen keeps: every auction lot has
    both, so the lenient rule would watch the entire site. The cap spends remaining
    slots on the soonest-ending survivors, where tracking can still change a decision.
    """
    candidates = []
    for entry in catalog.lots.values():
        if entry.watch or entry.state not in ("live", "ending"):
            continue
        result = prescreen(entry.to_listing(), vertical, require_photo=False)
        if result.keep and result.score >= 1:
            candidates.append((result.score, entry))
    room = max(0, cap - sum(
        1 for e in catalog.lots.values() if e.watch and e.state in ("live", "ending")
    ))
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda t: (-t[0], t[1].ends_at or far_future))
    for _score, entry in candidates[:room]:
        entry.watch = True
    return min(len(candidates), room)


def _make_client() -> EbthClient:
    """The default EBTH client for a real run — a browser fetcher unless EBTH_FETCH says
    otherwise. Split out as a seam so tests inject a fake without a browser."""
    return build_client()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Track EBTH auctions and publish bid guidance")
    ap.add_argument("--out", default="docs/auctions")
    ap.add_argument("--catalog", default="docs/auctions/catalog.json")
    ap.add_argument("--max-appraisals", type=int,
                    default=_int_env("MAX_AUCTION_APPRAISALS") or 6)
    ap.add_argument("--vertical", default=_env("VERTICAL", "furniture"))
    ap.add_argument("--probe", action="store_true",
                    help="fetch the configured pages and publish a structure report "
                         "instead of running the pipeline")
    ap.add_argument("--dry-run", action="store_true", help="no AI; track and render only")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    urls = _search_urls(_env("EBTH_SEARCH_URLS", _DEFAULT_SEARCHES))
    # A browser client owns a Chromium process; close it no matter how main() exits.
    client = _make_client()
    try:
        return _run(args, out_dir, urls, client)
    finally:
        client.close()


def _run(args, out_dir: Path, urls: list[str], client: EbthClient) -> int:
    if args.probe:
        return _probe(client, urls, out_dir)

    try:
        catalog = acat.load_auction_catalog(Path(args.catalog))
    except acat.AuctionCatalogCorrupt as exc:
        print(str(exc), file=sys.stderr)
        _write_status(out_dir, "catalog_corrupt")
        return 6

    if not args.dry_run:
        rc = _check_credentials(_env("APPRAISER_PROVIDER", "claude-code"))
        if rc:
            return rc

    vertical = get_vertical(args.vertical)
    now = datetime.now(timezone.utc)

    # 1. Fetch the search endpoint(s). This is BOTH discovery and snapshot: EBTH's search
    #    payload returns every matching lot with its live bid, count, and end time, so one
    #    page load refreshes the whole watchlist that's still in results and surfaces new
    #    lots at once. It runs every run — the endgame needs hourly bids, and a single
    #    headless page load per query is a rounding error to a site this size.
    searches_failed = 0
    seen_ids: set[str] = set()
    for url in urls:
        try:
            items = client.search(url)
        except Exception as exc:  # noqa: BLE001 — one search must not sink the run
            searches_failed += 1
            log.warning("ebth_search_failed", url=url, error=str(exc)[:200])
            continue
        seen_ids.update(i.item_id for i in items)
        rep = acat.observe_auctions(catalog, items, now=now)
        log.info("ebth_searched", url=url, items=len(items), new=rep.new,
                 snapshots=rep.snapshots)
    if urls and searches_failed < len(urls):
        catalog.last_discovery_at = now
    scan_failed = bool(urls) and searches_failed == len(urls)

    # 2. Watchlist refresh from what the searches surfaced.
    promoted = _refresh_watchlist(catalog, vertical, _int_env("EBTH_MAX_WATCH") or 40)
    if promoted:
        log.info("watchlist_promoted", count=promoted)

    # 3. Item-page snapshots — only for watched endgame lots the searches did NOT return
    #    (they scrolled off page one), since those are the ones whose bids would otherwise
    #    go stale exactly when they matter most. Best-effort: EBTH's item route renders its
    #    detail client-side in a shape the search payload already gives us in bulk, so a
    #    lot that yields nothing here simply keeps its last search-sourced bid, and the
    #    clock still finalizes it. This is why the pipeline never depends on item pages.
    snapshot_cap = _int_env("EBTH_SNAPSHOT_CAP") or 20
    snapped = gone = 0
    stale = [e for e in acat.snapshot_due(catalog, now=now)
             if e.id not in seen_ids and e.url]
    for entry in stale[:snapshot_cap]:
        try:
            item = client.item(entry.url)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                entry.state = "gone"
                gone += 1
            else:
                log.warning("ebth_snapshot_failed", lot=entry.id, error=str(exc)[:120])
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("ebth_snapshot_failed", lot=entry.id, error=str(exc)[:120])
            continue
        if item is not None:
            acat.observe_auctions(catalog, [item], now=now)
            snapped += 1

    # 3. Appraise the watchlist, once per lot, closest ending first.
    appraised = 0
    failures: list[str] = []
    todo = acat.unappraised_watch(catalog)[: args.max_appraisals]
    if todo and not args.dry_run:
        provider = get_appraiser(_env("APPRAISER_PROVIDER", "claude-code"))
        listings = [e.to_listing() for e in todo]
        photos = _download_photos(listings, out_dir / "_photos")
        photos_dir = out_dir / "photos"
        photos_dir.mkdir(parents=True, exist_ok=True)
        for entry in todo:
            listing = entry.to_listing()
            paths = photos.get(entry.id, [])
            try:
                appraisal = provider.appraise(listing, vertical, image_paths=paths or None)
            except Exception as exc:  # noqa: BLE001
                failures.append(str(exc))
                log.warning("auction_appraisal_failed", lot=entry.id, error=str(exc)[:200])
                continue
            acat.record_auction_appraisal(
                entry, appraisal, appraiser=provider.name,
                with_photos=bool(paths), now=now,
            )
            appraised += 1
            if paths:
                dest = photos_dir / f"{entry.id}{Path(paths[0]).suffix or '.jpg'}"
                Path(paths[0]).replace(dest)
                entry.photo_rel = f"photos/{dest.name}"

    # 4. Prune, then persist the expensive artifacts before any rendering can fail.
    removed = acat.prune_auctions(catalog, now=now)
    for rid in removed:
        for pattern in (f"{rid}.*", f"{rid}_[0-9].*"):
            for stale in (out_dir / "photos").glob(pattern):
                stale.unlink(missing_ok=True)
    acat.save_auction_catalog(catalog, Path(args.catalog))

    # 5. Guidance and the page.
    premium = float(_env("EBTH_PREMIUM_PCT", "0.15"))
    shipping = _int_env("EBTH_SHIPPING_CENTS") or 0
    hourly = _int_env("HOURLY_RATE_CENTS") or 3000
    pairs = acat.calibration_pairs(catalog)
    multiplier = bidding.endgame_multiplier(pairs)
    guidance = {}
    for entry in catalog.lots.values():
        if entry.appraisal is None or not entry.watch:
            continue
        g = bidding.guide(
            entry, multiplier=multiplier, calibration_n=len(pairs),
            hourly_rate_cents=hourly, premium_pct=premium,
            shipping_cents=shipping, now=now,
        )
        if g is not None:
            guidance[entry.id] = g

    state = "scan_blocked" if scan_failed else "ok"
    page = write_auction_page(
        catalog, guidance, out_dir,
        meta=AuctionBoardMeta(
            generated_at=f"updated {now.strftime('%b %d, %Y · %H:%M UTC')}",
            premium_pct=premium, multiplier=multiplier, calibration_n=len(pairs),
            state=state,
        ),
        now=now,
    )
    acat.save_auction_catalog(catalog, Path(args.catalog))

    ending = len(acat.ending_soon(catalog))
    print(
        f"lots {len(catalog.lots)} · watched {len(acat.watched(catalog))} · "
        f"ending≤24h {ending} · snapshots {snapped} · appraised {appraised} · "
        f"gone {gone} · wrote {page}"
    )
    if todo and not appraised and not args.dry_run and failures:
        print(f"All {len(todo)} auction appraisals failed.", file=sys.stderr)
        _write_status(out_dir, "appraisals_failed",
                      failed=len(todo), reason=failure_reason(failures))
        return 4
    _write_status(out_dir, state,
                  lots=len(catalog.lots), watched=len(acat.watched(catalog)),
                  ending_soon=ending, appraised=appraised,
                  actionable=sum(1 for g in guidance.values() if g.stance == "bid"))
    return 5 if scan_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
