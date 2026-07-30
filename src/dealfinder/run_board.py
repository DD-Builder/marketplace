"""One-command run: scrape → download photos → appraise → rank → write the site.

This is what GitHub Actions executes. Everything it needs comes from the environment, so
the workflow file stays trivial and no secrets live in the repo:

    APIFY_TOKEN               your Apify token (repo secret)
    CLAUDE_CODE_OAUTH_TOKEN   long-lived subscription token from `claude setup-token`
    APPRAISER_PROVIDER        claude-code (subscription, default) | claude-api
    SEARCH_URLS               newline/comma separated Marketplace search URLs
    MAX_APPRAISALS            hard cap on AI calls per run (cost control)

State lives in ``docs/catalog.json``, committed alongside the site. It is both the
cost-control ledger (so a later run skips listings already evaluated and only spends on
genuinely new pieces or price drops) *and* the archive of every appraisal, so the board
shows everything still for sale — not just the dozen pieces this particular run valued.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dealfinder import catalog as catalog_mod
from dealfinder.appraiser import get_appraiser
from dealfinder.board import BoardMeta, write_site
from dealfinder.engine import RunResult, evaluate_piece, run_valuation
from dealfinder.logging import get_logger
from dealfinder.pieces import costs_by_id, load_ledger
from dealfinder.ranking import TIERS
from dealfinder.selection import plan_appraisals
from dealfinder.sources.apify import (
    records_to_listings,
    recover_runs,
    run_and_fetch,
)
from dealfinder.sources.scrape import SearchFilters, scrape
from dealfinder.verticals import get_vertical

log = get_logger(__name__)

# Towns inside a ~40-mile radius of Lexington, KY. Override with IN_RADIUS_TOWNS.
_DEFAULT_RADIUS_TOWNS = (
    "lexington,nicholasville,georgetown,versailles,winchester,richmond,paris,frankfort,"
    "lawrenceburg,harrodsburg,salvisa,waco,berea,mt sterling,cynthiana,willisburg,"
    "mackville,wilmore,midway,stamping ground,sadieville,keene,athens"
)

# One search by default. Each additional search is a separate billed scrape, and the
# starting budget here is Apify's $5/month free credit — two searches at 150 results
# exhausted a whole month's allowance in two runs. Add more via SEARCH_URLS once you know
# what a run actually costs you.
_DEFAULT_SEARCH_URLS = "https://www.facebook.com/marketplace/lexington/search/?query=dresser"


def _env(name: str, default: str) -> str:
    """Read an env var, treating empty/whitespace as unset.

    GitHub Actions substitutes an unset repository variable as an empty string rather
    than omitting it, so ``os.getenv(name, default)`` returns "" and never the default —
    which then blows up ``int("")``. Anything optional must go through here.
    """
    return (os.getenv(name) or "").strip() or default


def _int_env(name: str) -> int | None:
    raw = _env(name, "")
    try:
        return int(raw) if raw else None
    except ValueError:
        log.warning("bad_int_env", name=name, value=raw)
        return None


def _filters() -> SearchFilters:
    """Search-URL filters, from the environment.

    Nearly a fifth of the measured scrape was spent on listings outside the price range or
    radius — money billed before we could throw the rows away. These push the cut back to
    Facebook. Defaults are deliberately wide; narrow them once you know your run's cost.
    """
    return SearchFilters(
        min_price_dollars=_int_env("MIN_PRICE_DOLLARS"),
        max_price_dollars=_int_env("MAX_PRICE_DOLLARS"),
        days_since_listed=_int_env("DAYS_SINCE_LISTED"),
        radius_km=_int_env("SEARCH_RADIUS_KM"),
        newest_first=_env("SORT_NEWEST_FIRST", "1") not in ("0", "false", "no"),
    )


def _radius_check(towns: str):
    names = [t.strip().lower() for t in towns.split(",") if t.strip()]

    def in_radius(location_text: str) -> bool:
        low = (location_text or "").lower()
        return any(n in low for n in names)

    return in_radius


def _download_photos(
    listings, out_dir: Path, per_listing: int = 3, timeout: float = 12.0
) -> dict[str, list[Path]]:
    """Grab photo bytes now — Facebook's signed URLs expire within hours.

    Fails fast and gives up entirely after a few consecutive failures: in a network that
    can't reach the photo CDN, retrying every URL just burns the job's time budget.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    got: dict[str, list[Path]] = {}
    consecutive_failures = 0
    for listing in listings:
        if consecutive_failures >= 6:
            log.warning("photo_download_abandoned", reason="photo host unreachable")
            break
        paths: list[Path] = []
        for i, photo in enumerate(listing.photos[:per_listing]):
            if not photo.remote_url.startswith("http"):
                # A local: sentinel from the catalogue — nothing to download, and it must
                # not count against the circuit breaker. The on-disk supplement covers it.
                continue
            dest = out_dir / f"{listing.fb_listing_id}_{i}.jpg"
            try:
                req = urllib.request.Request(
                    photo.remote_url, headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    dest.write_bytes(resp.read())
                paths.append(dest)
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001 — a missing photo isn't fatal
                consecutive_failures += 1
                log.warning("photo_failed", listing=listing.fb_listing_id, error=str(exc)[:120])
                break  # the rest of this listing's photos will fail the same way
        if paths:
            got[listing.fb_listing_id] = paths
    return got


def _reconcile_photos(catalog: catalog_mod.Catalog, photos_dir: Path) -> int:
    """Link photo files already on disk to entries that lost (or never had) photo_rel.

    The live board shipped with 20 committed photos belonging to entries whose
    photo_rel was never set — files paid for, present, and invisible. This runs at
    startup, is idempotent, and also removes files whose listing left the catalogue.
    """
    if not photos_dir.is_dir():
        return 0
    linked = 0
    for entry in catalog.listings.values():
        if not entry.photo_rel:
            match = sorted(photos_dir.glob(f"{entry.id}.*"))
            if match:
                entry.photo_rel = f"photos/{match[0].name}"
                linked += 1
        if not entry.extra_photo_rels:
            extras = sorted(photos_dir.glob(f"{entry.id}_[0-9].*"))
            if extras:
                entry.extra_photo_rels = [f"photos/{p.name}" for p in extras]
    for f in photos_dir.iterdir():
        if not f.is_file():
            continue
        base = f.stem
        if len(base) > 2 and base[-2] == "_" and base[-1].isdigit():
            base = base[:-2]  # strip a gallery suffix (_1, _2) only, never inner underscores
        if base not in catalog.listings:
            f.unlink()
    if linked:
        log.info("photos_reconciled", linked=linked)
    return linked


def _store_extra_photos(
    catalog: catalog_mod.Catalog, photos: dict[str, list[Path]], photos_dir: Path
) -> None:
    """Keep the gallery shots. The downloader fetches up to 3 per listing; only [0] used
    to survive — the workflow rm -rf'd the rest after paying to fetch them."""
    photos_dir.mkdir(parents=True, exist_ok=True)
    for lid, paths in photos.items():
        entry = catalog.listings.get(lid)
        if entry is None or len(paths) < 2:
            continue
        rels = []
        for i, src in enumerate(paths[1:3], start=1):
            src = Path(src)
            if not src.exists():
                continue
            dest = photos_dir / f"{lid}_{i}{src.suffix or '.jpg'}"
            if src.resolve() != dest.resolve():
                import shutil

                shutil.copyfile(src, dest)
            rels.append(f"photos/{dest.name}")
        if rels:
            entry.extra_photo_rels = rels


def _write_status(out_dir: Path, state: str, **counts) -> None:
    """A tiny machine-readable verdict the page reads, so a quota-blocked or auth-broken
    day shows a banner instead of a silently stale board."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "status.json").write_text(
            json.dumps({"state": state,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        **counts}, indent=1),
            encoding="utf-8",
        )
    except OSError as exc:  # a status file must never sink the run it describes
        log.warning("status_write_failed", error=str(exc)[:120])


def _load_seen(path: Path) -> dict[str, int | None]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("seen_ledger_unreadable", path=str(path))
    return {}


def _open_catalog(catalog_path: Path, seen_path: Path) -> catalog_mod.Catalog:
    """Load the catalogue, seeding it from the old flat ledger the first time.

    Without the migration the switch-over would treat every previously-seen listing as
    brand new and re-appraise the lot — one silent run costing a whole cap of AI calls.
    """
    if catalog_path.exists():
        return catalog_mod.load_catalog(catalog_path)
    legacy = _load_seen(seen_path)
    if legacy:
        log.info("catalog_migrated_from_seen", entries=len(legacy))
        return catalog_mod.migrate_from_seen(legacy)
    return catalog_mod.Catalog()


def _check_credentials(provider: str) -> int:
    """Return a non-zero exit code (and explain) if the chosen appraiser can't authenticate.

    Only enforced in CI. Locally the Claude Code CLI carries its own stored login, so
    demanding the env var there would reject a perfectly working setup.
    """
    in_ci = os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"
    if (
        in_ci
        and provider == "claude-code"
        and not os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    ):
        print(
            "CLAUDE_CODE_OAUTH_TOKEN is empty — the subscription appraiser cannot "
            "authenticate, so every valuation would fail.\n"
            "Fix: add a repository SECRET (not a variable) named exactly "
            "CLAUDE_CODE_OAUTH_TOKEN, holding the sk-ant-oat... value printed by "
            "`claude setup-token`.",
            file=sys.stderr,
        )
        return 3
    if provider == "claude-api" and not os.getenv("ANTHROPIC_API_KEY", "").strip():
        print("APPRAISER_PROVIDER=claude-api but ANTHROPIC_API_KEY is empty.", file=sys.stderr)
        return 3
    return 0


def _search_urls(raw: str) -> list[str]:
    import re

    parts: list[str] = []
    for line in raw.split("\n"):
        # A comma separates URLs only when the next chunk starts one — a comma *inside*
        # a query value must not split the URL into two garbage halves.
        parts += [p.strip() for p in re.split(r",(?=\s*https?://)", line.strip())]
    return [p for p in parts if p.startswith("http")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scrape, appraise, and publish the deal board")
    ap.add_argument("--out", default="docs", help="site output dir (GitHub Pages serves this)")
    ap.add_argument("--catalog", default="docs/catalog.json",
                    help="persistent catalogue: seen-ledger + stored appraisals")
    ap.add_argument("--seen", default="docs/seen.json",
                    help="legacy flat ledger; read once to seed a missing catalogue")
    ap.add_argument("--pieces", default="docs/pieces.json",
                    help="your books: price paid, materials, hours, sale price")
    ap.add_argument("--limit", type=int, default=_int_env("RESULTS_LIMIT") or 60,
                    help="listings to request per search URL (drives most of the scrape cost)")
    ap.add_argument("--max-appraisals", type=int,
                    default=_int_env("MAX_APPRAISALS") or 12,
                    help="hard cap on AI calls this run")
    ap.add_argument("--wildcards", type=int, default=_int_env("WILDCARDS") or 3)
    ap.add_argument("--vertical", default=_env("VERTICAL", "furniture"))
    ap.add_argument("--from-json", default="", help="use a local JSON export instead of scraping")
    ap.add_argument("--recover", action="store_true",
                    help="re-read the datasets your past Apify runs already produced "
                         "instead of scraping. Reading a stored dataset starts no actor "
                         "and costs no credit — use this to rescue a scrape you paid for")
    ap.add_argument("--recover-limit", type=int, default=20,
                    help="how many recent runs to pull back with --recover")
    ap.add_argument("--dry-run", action="store_true", help="skip AI; render pre-screen only")
    ap.add_argument("--no-photos", action="store_true",
                    help="value from text alone (for networks that can't reach the photo "
                         "CDN); such valuations are marked blind and redone later")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    catalog_path = Path(args.catalog)
    try:
        catalog = _open_catalog(catalog_path, Path(args.seen))
    except catalog_mod.CatalogCorrupt as exc:
        # Refusing to run is the fix: proceeding would save an empty catalogue over the
        # damaged one and destroy every stored appraisal.
        print(str(exc), file=sys.stderr)
        _write_status(out_dir, "catalog_corrupt")
        return 6
    _reconcile_photos(catalog, out_dir / "photos")
    seen = catalog_mod.seen_view(catalog)
    ledger = load_ledger(Path(args.pieces))
    logged = costs_by_id(ledger)
    vertical = get_vertical(args.vertical)

    # Check credentials before spending a scrape. A missing one otherwise surfaces as
    # every appraisal failing one by one — which reads like a model problem rather than a
    # configuration mistake, and by then the Apify credit is already gone.
    if not args.dry_run:
        rc = _check_credentials(_env("APPRAISER_PROVIDER", "claude-code"))
        if rc:
            return rc

    # 1. Get listings — from Apify, or a local export for testing.
    coverage: dict[str, catalog_mod.SearchCoverage] = {}
    scan_failed = False
    if args.from_json:
        records = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        listings = records_to_listings(records)
    elif args.recover:
        token = os.getenv("APIFY_TOKEN", "").strip()
        if not token:
            print("APIFY_TOKEN is not set — cannot reach your past runs.", file=sys.stderr)
            return 2
        listings, report = recover_runs(
            # Scoped to the scraping actor: an account may hold runs of unrelated actors
            # whose records would pollute the catalogue.
            token=token,
            actor=_env("APIFY_ACTOR", "apify~facebook-marketplace-scraper"),
            limit=args.recover_limit,
        )
        for run in report:
            log.info("recovered_run", run=run["id"], started=run["started_at"],
                     status=run["status"], listings=run["recovered"],
                     error=run["error"] or None)
        expired = [r for r in report if r["error"] or not r["recovered"]]
        print(
            f"recovered {len(listings)} listings from {len(report) - len(expired)} of "
            f"{len(report)} past runs"
            + (f" ({len(expired)} expired or empty)" if expired else "")
        )
        if not listings:
            print(
                "Nothing came back. Apify keeps a run's dataset for a limited time — "
                "these have most likely expired, so the data is gone and only a fresh "
                "scrape will replace it.",
                file=sys.stderr,
            )
            return 5
    else:
        token = os.getenv("APIFY_TOKEN", "").strip()
        if not token:
            print("APIFY_TOKEN is not set — cannot scrape.", file=sys.stderr)
            return 2
        urls = _search_urls(_env("SEARCH_URLS", _DEFAULT_SEARCH_URLS))
        if not urls:
            print("SEARCH_URLS is set but contains no http(s) URLs.", file=sys.stderr)
            return 2
        def fetch(run_input: dict, actor_id: str):
            return run_and_fetch(run_input, token=token, actor=actor_id)

        scraped = scrape(
            urls, seen,
            fetch=fetch,
            already_detailed=catalog_mod.detailed_ids(catalog),
            actor=_env("APIFY_ACTOR", "apify~facebook-marketplace-scraper"),
            detail_actor=_env("APIFY_DETAIL_ACTOR", "") or None,
            results_limit=args.limit,
            # Over-fetch the appraisal cap: pre-screening on a thin record is imperfect, so
            # a slightly wider detail net keeps good pieces from being cut on a bad title.
            detail_cap=int(args.max_appraisals * 1.5) + 2,
            detail_supported=catalog.meta.detail_fetch_supported,
            filters=_filters(),
        )
        listings = scraped.listings
        coverage = scraped.coverage
        catalog_mod.record_capability(catalog, scraped.detail_supported)
        log.info("scrape", summary=scraped.summary())
        if scraped.searches_failed and not listings:
            print(
                "Every search failed, so nothing new was seen this run:\n  "
                + "\n  ".join(scraped.searches_failed)
                + "\nRebuilding the board from the catalogue instead.",
                file=sys.stderr,
            )
            scan_failed = True
    log.info("scraped", count=len(listings))

    # 2. Fold the scan into the catalogue *before* planning, so price history, sold/gone
    #    state and first-seen dates are recorded even for listings we never pay to value.
    #    `seen` was snapshotted above, so the diff still sees pre-scan prices.
    if scan_failed:
        # A scan that never reached Marketplace is not evidence that anything is missing.
        # observe() counts a miss against every live entry it doesn't see, so running it on
        # a quota-blocked day would retire listings we simply never looked for.
        obs = catalog_mod.ObserveReport()
        log.info("observe_skipped", reason="no search reached Marketplace")
    else:
        # Recovered datasets and local exports carry no absence evidence: they mention
        # the listings they mention and imply nothing about the rest of the market.
        obs = catalog_mod.observe(
            catalog, listings, coverage=coverage,
            absence_evidence=not (args.from_json or args.recover),
        )
    log.info("observed", new=obs.new, price_drops=obs.price_drops, gone=obs.marked_gone,
             sold=obs.marked_sold)

    # 3. Cost-controlled selection happens inside the engine; but photos must be fetched
    #    for the pieces that will actually be appraised, so we plan first. `plan_appraisals`
    #    is pure, so this plan is identical to the one the engine computes.
    # MAX_APPRAISALS is the hard total. Wildcards come out of it, never on top of it —
    # previously cap 3 + wildcards 3 quietly spent 4.
    wildcards = min(args.wildcards, max(args.max_appraisals - 1, 0))
    top_n = max(args.max_appraisals - wildcards, 1)
    # A price drop on a piece we already valued re-ranks for free — the object didn't
    # change, only what it costs us. Two things do earn a second look: a thin record that
    # just gained a description, and a piece we valued blind now that we can show the
    # model a photograph. Those redo candidates must be IN the pool, not merely un-skipped:
    # they are neither new (the seen-diff ignores them) nor unappraised (backfill ignores
    # them), so excluding them from `valued` alone leaves them unreachable.
    redo_ids = set(obs.detail_upgrades)
    # Appraisals from a superseded prompt generation are stale answers, not answers.
    redo_ids |= catalog_mod.stale_appraisals(catalog)
    if not args.no_photos:
        redo_ids |= {
            i for i in catalog_mod.blind_appraisals(catalog)
            if catalog.listings[i].photo_urls and catalog.listings[i].state == "live"
        }
    redo_listings = [
        catalog.listings[i].to_listing() for i in sorted(redo_ids) if i in catalog.listings
    ]
    pool = catalog_mod.unappraised_live(catalog)
    # Entries with a photo already on disk first: they can be valued with vision TODAY,
    # even on a day the scrape is quota-blocked, while the rest would be valued blind.
    pool.sort(key=lambda l: catalog.listings[l.fb_listing_id].photo_rel is None)
    backfill = redo_listings + pool
    valued = catalog_mod.already_valued(catalog, exclude=redo_ids)
    plan = plan_appraisals(
        listings, seen, vertical=vertical,
        top_n=top_n, wildcards=wildcards, backfill=backfill, already_valued=valued,
    )
    log.info("plan", summary=plan.summary())

    photos = (
        {} if args.dry_run or args.no_photos
        else _download_photos(plan.to_appraise, out_dir / "_photos")
    )
    if not (args.dry_run or args.no_photos):
        # Photos already on disk from an earlier run serve the appraiser too. The CDN
        # URLs for these expired within hours, but the pixels never went anywhere —
        # without this, a piece whose photo we committed weeks ago was valued blind.
        for lst in plan.to_appraise:
            lid = lst.fb_listing_id
            if lid not in photos:
                on_disk = sorted((out_dir / "photos").glob(f"{lid}.*")) + sorted(
                    (out_dir / "photos").glob(f"{lid}_[0-9].*")
                )
                if on_disk:
                    photos[lid] = on_disk[:3]

    # 4. Appraise (or stub in dry-run) and rank.
    if args.dry_run:
        from dealfinder.core.schemas import AppraisalResult

        class _Dry:
            name = "dry-run"

            def appraise(self, listing, vertical, *, image_paths=None):
                ask = listing.asking_price_cents or 0
                return AppraisalResult(
                    identified_item="unappraised",
                    est_asis_value_cents=ask,
                    est_restored_resale_value_cents=ask,
                    est_restoration_cost_cents=0,
                    est_restoration_effort_hours=0.0,
                    confidence=0.0,
                    deal_score=0.0,
                    reasoning="dry run — no AI called",
                )

        provider = _Dry()
    else:
        provider = get_appraiser(_env("APPRAISER_PROVIDER", "claude-code"))
    log.info("appraiser", provider=provider.name)

    hourly = _int_env("HOURLY_RATE_CENTS") or 3000
    in_radius = _radius_check(_env("IN_RADIUS_TOWNS", _DEFAULT_RADIUS_TOWNS))
    result = run_valuation(
        listings, seen,
        provider=provider,
        vertical=vertical,
        hourly_rate_cents=hourly,
        top_n=top_n,
        wildcards=wildcards,
        in_radius=in_radius,
        image_paths_by_id={k: v for k, v in photos.items()} if photos else None,
        backfill=backfill,
        already_valued=valued,
    )

    # 5. Store this run's appraisals, then render the *accumulated* board.
    cover = {lid: paths[0] for lid, paths in photos.items() if paths}

    if not args.dry_run:
        # A dry run's stub appraisals must never be recorded: they would satisfy
        # "already valued" forever and permanently poison the entries they touch.
        catalog_mod.record_appraisals(
            catalog, result.pieces, appraiser=provider.name,
            photo_rel={lid: f"photos/{lid}{Path(p).suffix or '.jpg'}"
                       for lid, p in cover.items()},
            saw_photos=photos.keys(),
        )
    pruned = catalog_mod.prune(
        catalog, photo_retention_days=_int_env("PHOTO_RETENTION_DAYS") or 30
    )
    # Photos of departed entries, and of entries whose pictures have aged out. Both are
    # deleted the same way: by exact stem, since `{id}*` would also match ids that merely
    # start with this one.
    for gone_id in pruned.removed_ids + pruned.expired_photo_ids:
        for pattern in (f"{gone_id}.*", f"{gone_id}_[0-9].*"):
            for stale in (out_dir / "photos").glob(pattern):
                stale.unlink(missing_ok=True)
    if pruned.expired_photo_ids:
        log.info("photos_expired", count=len(pruned.expired_photo_ids))
    # Persist the expensive artifact *now*, before rendering. A crash anywhere in the
    # photo/board code below must not cost the appraisals this run just paid for.
    catalog_mod.save_catalog(catalog, catalog_path)

    # Photos for pieces that will be on the board but have none yet. Previously only the
    # listings appraised *this* run were fetched, so a piece valued last week showed a grey
    # placeholder forever. Facebook's URLs expire within hours, so old ones simply fail —
    # that's fine, the downloader tolerates it and a fresh scrape supplies working ones.
    if not (args.dry_run or args.no_photos):
        need_photos = [
            e.to_listing() for e in catalog_mod.live_entries(catalog)
            if not e.photo_rel and e.photo_urls
        ][: _int_env("MAX_PHOTO_BACKFILL") or 40]
        if need_photos:
            got = _download_photos(need_photos, out_dir / "_photos")
            log.info("photo_backfill", wanted=len(need_photos), got=len(got))
            photos.update(got)
            for lid, paths in got.items():
                entry = catalog.listings.get(lid)
                if entry and paths:
                    entry.photo_rel = f"photos/{lid}{Path(paths[0]).suffix or '.jpg'}"
            cover.update({lid: paths[0] for lid, paths in got.items() if paths})

    # Gallery shots for everything downloaded this run (appraisal + backfill alike).
    if photos:
        _store_extra_photos(catalog, photos, out_dir / "photos")

    # Every live, appraised piece — not just this run's dozen — re-scored against today's
    # price and against how long ago we last confirmed it was still for sale.
    now_utc = datetime.now(timezone.utc)
    board_pieces = []
    for entry in catalog_mod.live_entries(catalog):
        try:
            board_pieces.append(
                evaluate_piece(entry.to_listing(), entry.appraisal,
                               hourly_rate_cents=hourly, in_radius=in_radius,
                               logged_costs=logged.get(entry.id), vertical=vertical,
                               days_since_seen=max(
                                   0.0, (now_utc - entry.last_seen).total_seconds() / 86400))
            )
        except Exception as exc:  # noqa: BLE001 — a bad entry shouldn't blank the board
            log.warning("catalog_entry_skipped", listing=entry.id, error=str(exc)[:120])
    board_pieces.sort(key=lambda p: p.priority, reverse=True)
    # Cap *per tier*, not overall. One global ranking is won by whatever has the best
    # ratio, and a $10 nightstand worth $50 beats a $220 credenza worth $1,500 on every
    # percentage measure — so the pieces actually worth driving for get buried by volume.
    per_tier = _int_env("MAX_CARDS_PER_TIER") or 8
    kept: list = []
    for tier_key, _label, _blurb in TIERS:
        kept += [p for p in board_pieces if p.tier == tier_key][:per_tier]
    board_pieces = kept

    now = datetime.now(timezone.utc)
    page = write_site(
        RunResult(pieces=board_pieces, plan=plan), out_dir,
        meta=BoardMeta(
            region=_env("REGION_LABEL", "Lexington · 40 mi"),
            generated_at=f"updated {now.strftime('%b %d, %Y · %H:%M UTC')}",
            generated_at_iso=now.isoformat(),
            note=f"Valued by {provider.name}. Photos and prices as scraped; verify before buying.",
            # In Actions these come free; locally you can set them to make the page's
            # buttons work against your repo. Empty renders a read-only board that says so.
            repo=_env("GITHUB_REPOSITORY", ""),
            branch=_env("BOARD_BRANCH", "") or _env("GITHUB_REF_NAME", "main"),
            pieces_path=args.pieces,
            drafts_dir=_env("DRAFTS_DIR", ".drafts"),
        ),
        photo_files=cover,
        extra_photo_map={
            e.id: e.photo_rel for e in catalog_mod.live_entries(catalog) if e.photo_rel
        },
        gallery_map={
            e.id: e.extra_photo_rels
            for e in catalog_mod.live_entries(catalog) if e.extra_photo_rels
        },
    )
    catalog_mod.save_catalog(catalog, catalog_path)
    # Pages runs Jekyll by default, which drops files and directories starting with an
    # underscore. Nothing here needs Jekyll, and the marker costs nothing.
    (out_dir / ".nojekyll").touch()

    print(plan.summary())
    print(
        f"appraised {len(result.pieces)} this run · catalogue {len(catalog.listings)} "
        f"({len(board_pieces)} on board) · {len(result.killers)} killers · wrote {page}"
    )
    # A run that selected pieces but valued none of them is a failure, even though each
    # individual error is caught so one bad listing can't sink the batch. Reporting success
    # here published an empty board and hid a missing credential. It outranks a failed scan
    # below because a dead credential needs fixing now; a spent quota just needs waiting.
    if plan.to_appraise and not result.pieces and not args.dry_run:
        print(
            f"\nAll {len(plan.to_appraise)} appraisals failed — the board is empty. "
            "See the appraisal_failed warnings above for the reason.",
            file=sys.stderr,
        )
        _write_status(out_dir, "appraisals_failed",
                      failed=len(plan.to_appraise), on_board=len(board_pieces))
        return 4
    # The board is up to date either way, but a run that couldn't reach Marketplace still
    # exits non-zero — a silent success would hide a scraper that has stopped working.
    _write_status(out_dir, "scan_blocked" if scan_failed else "ok",
                  on_board=len(board_pieces), appraised=len(result.pieces))
    return 5 if scan_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
