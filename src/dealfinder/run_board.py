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
from dealfinder.selection import plan_appraisals
from dealfinder.sources.apify import records_to_listings, run_and_fetch
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


def _load_seen(path: Path) -> dict[str, int | None]:
    if path.exists():
        try:
            return json.loads(path.read_text())
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
    parts = [p.strip() for chunk in raw.split("\n") for p in chunk.split(",")]
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
    ap.add_argument("--limit", type=int, default=int(_env("RESULTS_LIMIT", "60")),
                    help="listings to request per search URL (drives most of the scrape cost)")
    ap.add_argument("--max-appraisals", type=int,
                    default=int(_env("MAX_APPRAISALS", "12")),
                    help="hard cap on AI calls this run")
    ap.add_argument("--wildcards", type=int, default=int(_env("WILDCARDS", "3")))
    ap.add_argument("--vertical", default=_env("VERTICAL", "furniture"))
    ap.add_argument("--from-json", default="", help="use a local JSON export instead of scraping")
    ap.add_argument("--dry-run", action="store_true", help="skip AI; render pre-screen only")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    catalog_path = Path(args.catalog)
    catalog = _open_catalog(catalog_path, Path(args.seen))
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
    if args.from_json:
        records = json.loads(Path(args.from_json).read_text())
        listings = records_to_listings(records)
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
                "Every search failed, so there is nothing to appraise:\n  "
                + "\n  ".join(scraped.searches_failed),
                file=sys.stderr,
            )
            return 5
    log.info("scraped", count=len(listings))

    # 2. Fold the scan into the catalogue *before* planning, so price history, sold/gone
    #    state and first-seen dates are recorded even for listings we never pay to value.
    #    `seen` was snapshotted above, so the diff still sees pre-scan prices.
    obs = catalog_mod.observe(catalog, listings, coverage=coverage)
    log.info("observed", new=obs.new, price_drops=obs.price_drops, gone=obs.marked_gone,
             sold=obs.marked_sold)

    # 3. Cost-controlled selection happens inside the engine; but photos must be fetched
    #    for the pieces that will actually be appraised, so we plan first. `plan_appraisals`
    #    is pure, so this plan is identical to the one the engine computes.
    top_n = max(args.max_appraisals - args.wildcards, 1)
    backfill = catalog_mod.unappraised_live(catalog)
    # A price drop on a piece we already valued re-ranks for free — the object didn't
    # change, only what it costs us. Only a thin record that just gained a description and
    # photos is worth a second look.
    valued = catalog_mod.already_valued(catalog, exclude=obs.detail_upgrades)
    plan = plan_appraisals(
        listings, seen, vertical=vertical,
        top_n=top_n, wildcards=args.wildcards, backfill=backfill, already_valued=valued,
    )
    log.info("plan", summary=plan.summary())

    photos = {} if args.dry_run else _download_photos(plan.to_appraise, out_dir / "_photos")

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

    hourly = int(_env("HOURLY_RATE_CENTS", "3000"))
    in_radius = _radius_check(_env("IN_RADIUS_TOWNS", _DEFAULT_RADIUS_TOWNS))
    result = run_valuation(
        listings, seen,
        provider=provider,
        vertical=vertical,
        hourly_rate_cents=hourly,
        top_n=top_n,
        wildcards=args.wildcards,
        in_radius=in_radius,
        image_paths_by_id={k: v for k, v in photos.items()} if photos else None,
        backfill=backfill,
        already_valued=valued,
    )

    # 5. Store this run's appraisals, then render the *accumulated* board.
    cover = {lid: paths[0] for lid, paths in photos.items() if paths}
    catalog_mod.record_appraisals(
        catalog, result.pieces, appraiser=provider.name,
        photo_rel={lid: f"photos/{lid}{Path(p).suffix or '.jpg'}" for lid, p in cover.items()},
    )
    pruned = catalog_mod.prune(catalog)
    for gone_id in pruned.removed_ids:
        for stale in (out_dir / "photos").glob(f"{gone_id}.*"):
            stale.unlink(missing_ok=True)

    # Every live, appraised piece — not just this run's dozen — re-scored against today's
    # price. This is why a piece found last week is still on the board this week.
    board_pieces = []
    for entry in catalog_mod.live_entries(catalog):
        try:
            board_pieces.append(
                evaluate_piece(entry.to_listing(), entry.appraisal,
                               hourly_rate_cents=hourly, in_radius=in_radius,
                               logged_costs=logged.get(entry.id))
            )
        except Exception as exc:  # noqa: BLE001 — a bad entry shouldn't blank the board
            log.warning("catalog_entry_skipped", listing=entry.id, error=str(exc)[:120])
    board_pieces.sort(key=lambda p: p.priority, reverse=True)
    board_pieces = board_pieces[: int(_env("MAX_CARDS", "150"))]

    now = datetime.now(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
    page = write_site(
        RunResult(pieces=board_pieces, plan=plan), out_dir,
        meta=BoardMeta(
            region=_env("REGION_LABEL", "Lexington · 40 mi"),
            generated_at=f"updated {now}",
            note=f"Valued by {provider.name}. Photos and prices as scraped; verify before buying.",
        ),
        photo_files=cover,
        extra_photo_map={
            e.id: e.photo_rel for e in catalog_mod.live_entries(catalog) if e.photo_rel
        },
    )
    catalog_mod.save_catalog(catalog, catalog_path)

    print(plan.summary())
    print(
        f"appraised {len(result.pieces)} this run · catalogue {len(catalog.listings)} "
        f"({len(board_pieces)} on board) · {len(result.killers)} killers · wrote {page}"
    )

    # A run that selected pieces but valued none of them is a failure, even though each
    # individual error is caught so one bad listing can't sink the batch. Reporting success
    # here published an empty board and hid a missing credential.
    if plan.to_appraise and not result.pieces and not args.dry_run:
        print(
            f"\nAll {len(plan.to_appraise)} appraisals failed — the board is empty. "
            "See the appraisal_failed warnings above for the reason.",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
