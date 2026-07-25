"""One-command run: scrape → download photos → appraise → rank → write the site.

This is what GitHub Actions executes. Everything it needs comes from the environment, so
the workflow file stays trivial and no secrets live in the repo:

    APIFY_TOKEN               your Apify token (repo secret)
    CLAUDE_CODE_OAUTH_TOKEN   long-lived subscription token from `claude setup-token`
    APPRAISER_PROVIDER        claude-code (subscription, default) | claude-api
    SEARCH_URLS               newline/comma separated Marketplace search URLs
    MAX_APPRAISALS            hard cap on AI calls per run (cost control)

The seen-ledger is a JSON file committed alongside the site, so a later run skips listings
already evaluated and only spends on genuinely new pieces or price drops.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dealfinder.appraiser import get_appraiser
from dealfinder.board import BoardMeta, write_site
from dealfinder.engine import run_valuation
from dealfinder.logging import get_logger
from dealfinder.selection import update_seen
from dealfinder.sources.apify import records_to_listings, run_and_fetch
from dealfinder.verticals import get_vertical

log = get_logger(__name__)

# Towns inside a ~40-mile radius of Lexington, KY. Override with IN_RADIUS_TOWNS.
_DEFAULT_RADIUS_TOWNS = (
    "lexington,nicholasville,georgetown,versailles,winchester,richmond,paris,frankfort,"
    "lawrenceburg,harrodsburg,salvisa,waco,berea,mt sterling,cynthiana,willisburg,"
    "mackville,wilmore,midway,stamping ground,sadieville,keene,athens"
)

# Searches to run when SEARCH_URLS isn't configured, so a fresh repo works out of the
# box. Overlap between these is free — results are deduped before anything is paid for.
_DEFAULT_SEARCH_URLS = (
    "https://www.facebook.com/marketplace/lexington/search/?query=dresser\n"
    "https://www.facebook.com/marketplace/lexington/search/?query=mid%20century"
)


def _env(name: str, default: str) -> str:
    """Read an env var, treating empty/whitespace as unset.

    GitHub Actions substitutes an unset repository variable as an empty string rather
    than omitting it, so ``os.getenv(name, default)`` returns "" and never the default —
    which then blows up ``int("")``. Anything optional must go through here.
    """
    return (os.getenv(name) or "").strip() or default


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


def _check_credentials(provider: str) -> int:
    """Return a non-zero exit code (and explain) if the chosen appraiser can't authenticate."""
    if provider == "claude-code" and not os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
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
    ap.add_argument("--seen", default="docs/seen.json", help="cross-run ledger path")
    ap.add_argument("--limit", type=int, default=int(_env("RESULTS_LIMIT", "150")),
                    help="listings to request per search URL")
    ap.add_argument("--max-appraisals", type=int,
                    default=int(_env("MAX_APPRAISALS", "12")),
                    help="hard cap on AI calls this run")
    ap.add_argument("--wildcards", type=int, default=int(_env("WILDCARDS", "3")))
    ap.add_argument("--vertical", default=_env("VERTICAL", "furniture"))
    ap.add_argument("--from-json", default="", help="use a local JSON export instead of scraping")
    ap.add_argument("--dry-run", action="store_true", help="skip AI; render pre-screen only")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    seen_path = Path(args.seen)
    seen = _load_seen(seen_path)
    vertical = get_vertical(args.vertical)

    # Check credentials before spending a scrape. A missing one otherwise surfaces as
    # every appraisal failing one by one — which reads like a model problem rather than a
    # configuration mistake, and by then the Apify credit is already gone.
    if not args.dry_run:
        rc = _check_credentials(_env("APPRAISER_PROVIDER", "claude-code"))
        if rc:
            return rc

    # 1. Get listings — from Apify, or a local export for testing.
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
        listings = []
        for url in urls:
            log.info("scraping", url=url)
            listings += run_and_fetch(
                {
                    "startUrls": [{"url": url}],
                    "resultsLimit": args.limit,
                    "includeListingDetails": True,
                },
                token=token,
                actor=_env("APIFY_ACTOR", "apify~facebook-marketplace-scraper"),
            )
    log.info("scraped", count=len(listings))

    # 2. Cost-controlled selection happens inside the engine; but photos must be fetched
    #    for the pieces that will actually be appraised, so we plan first.
    from dealfinder.selection import plan_appraisals

    plan = plan_appraisals(
        listings, seen, vertical=vertical,
        top_n=max(args.max_appraisals - args.wildcards, 1), wildcards=args.wildcards,
    )
    log.info("plan", summary=plan.summary())

    photos = {} if args.dry_run else _download_photos(plan.to_appraise, out_dir / "_photos")

    # 3. Appraise (or stub in dry-run) and rank.
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

    result = run_valuation(
        listings, seen,
        provider=provider,
        vertical=vertical,
        hourly_rate_cents=int(_env("HOURLY_RATE_CENTS", "3000")),
        top_n=max(args.max_appraisals - args.wildcards, 1),
        wildcards=args.wildcards,
        in_radius=_radius_check(_env("IN_RADIUS_TOWNS", _DEFAULT_RADIUS_TOWNS)),
        image_paths_by_id={k: v for k, v in photos.items()} if photos else None,
    )

    # 4. Render the site and advance the ledger.
    now = datetime.now(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
    cover = {lid: paths[0] for lid, paths in photos.items() if paths}
    page = write_site(
        result, out_dir,
        meta=BoardMeta(
            region=_env("REGION_LABEL", "Lexington · 40 mi"),
            generated_at=f"updated {now}",
            note=f"Valued by {provider.name}. Photos and prices as scraped; verify before buying.",
        ),
        photo_files=cover,
    )
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(update_seen(seen, listings), indent=0, sort_keys=True))

    print(plan.summary())
    print(f"appraised {len(result.pieces)} · {len(result.killers)} killers · wrote {page}")

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
