"""One-command auction run: discover lots → snapshot bids → appraise → advise → publish.

The cadence trick: this runs *hourly* (auctions are won and lost inside a day), but an
hourly job must not cost hourly money. So each run is asymmetric —

* **search** (both discovery of new lots and a bid/count/end-time refresh of everything
  still in results — EBTH's search response carries all three) happens every run, walking
  EBTH's *own* category tree under their *own* sort presets (ending soonest, highest bid,
  most bids), narrowed to the decision window; plus two site-wide sweeps that catch
  whatever the filing missed (see ``_AUTO_ENDING_SOON``/``_AUTO_RECOMMENDED``);
* **item-page snapshots** (best-effort, only for watched lots the searches didn't
  surface) happen every run too, endgame lots first;
* **appraisals** (the only expensive step) happen once per lot, capped per run.

Environment, mirroring run_board:

    CLAUDE_CODE_OAUTH_TOKEN   subscription appraiser auth (CI)
    APPRAISER_PROVIDER        claude-code (default) | claude-api
    EBTH_CATEGORIES           which of EBTH's own categories to trawl (ids, slugs or
                              names; empty = every category we can honestly price).
                              e.g. "Jewelry and Watches, Furniture" or "3313,3472"
    EBTH_SORTS                which of EBTH's sort presets to pull per category
                              (default ending_soon,highest_bid,most_bids)
    EBTH_SEARCH_URLS          literal browse URLs, overriding category discovery
                              entirely; screened under --vertical/VERTICAL
    VERTICAL                  vertical for --vertical / for EBTH_SEARCH_URLS overrides
    EBTH_TIME_CRITICAL_DAYS   the site-wide "closing soon" window (default 2) — lots
                              found this way are watched with no keyword gate at all;
                              being about to close is itself the reason to look
    EBTH_PREMIUM_PCT          buyer's premium assumption (default 0.15)
    EBTH_SHIPPING_CENTS       per-lot freight assumption (default 0 = local pickup)
    APPRAISE_MODEL            model for the valuation call (default claude-sonnet-5)
    MAX_AUCTION_APPRAISALS    AI-call cap per run (default 20). Not a budget so much as
                              a blast radius: the real ceiling is the watchlist, since a
                              lot is valued once and only inside the decision window, so
                              this caps how fast a backlog is worked off, not how much
                              gets valued in total.
    EBTH_SNAPSHOT_CAP         item-page fetches per run (default 20)
    EBTH_MAX_WATCH            watchlist size cap (default 150) — the actual limit on how
                              many lots ever get valued

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
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dealfinder.auctions import bidding
from dealfinder.auctions.categories import SORTS, resolve as resolve_categories
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
from dealfinder.verticals import all_verticals, get_vertical

log = get_logger(__name__)

# EBTH's search box submits GET /browse?q=... (confirmed off the live DOM — the header
# search form's action is /browse, not /search; /search silently degrades to browsing
# the entire unfiltered catalogue, which is how an early version of this pipeline ended
# up treating all 6,148 live lots as one query's results).
_EBTH_BASE = "https://www.ebth.com"

#: Pages to follow per discovery target. One, deliberately: every target is already
#: narrowed to a category AND the decision window AND one of EBTH's own sort orders, so
#: page 1 is the most relevant 48 lots by the house's own reckoning. With ~44 targets a
#: run, following three pages each would mean 130+ browser navigations an hour to reach
#: lots that are, by construction, the ones the sort ranked lowest.
#: Pages to walk per discovery query. One page is 96 lots. With per-category discovery
#: working again the queries are genuinely distinct, so depth multiplies against the
#: category count rather than replacing it. Two pages across ~14 categories on three
#: sorts is ~86 navigations at roughly 4s each — near six minutes, which is what the
#: hourly job's 30-minute budget can spend on discovery and still leave room for
#: valuations. EBTH_SORTS trims the sorts if that proves tight.
_DISCOVERY_MAX_PAGES = 2

#: A keyword search can only ever find lots that happen to contain a guessed word — it
#: has no way to notice a jewelry lot that doesn't say "sterling" or a rug that doesn't
#: say "hand-knotted", and it can't tell you what's about to close. So on top of the
#: per-vertical keyword queries, two site-wide sources are always run, using EBTH's own
#: ``sort``/``days_left`` parameters (confirmed live: sort=sale_ends_at_asc is "Ending
#: Soonest"; days_left genuinely narrows total_items — 1→1,242, 2→2,017, 3→3,200 site-
#: wide when checked). Lots found this way carry no category, so a vertical is
#: auto-classified per lot for pricing and appraisal guidance.
#:
#: These sources used to bypass the quality gate outright and outrank every real category
#: match, on the theory that urgency is itself a signal. That was wrong, and the board
#: showed exactly how wrong: the site-wide ending-soon feed is whatever EBTH happens to
#: be closing next — mass-market Barbie lots, boxed Christmas ornaments, nutcrackers —
#: and it filled the entire watchlist with them while 584 discovered jewelry lots got no
#: slot at all. Urgency says *when* to look at a lot, never *whether* it is worth
#: owning. They are ordinary discovery sources now and pass the same gate as everything
#: else.
_AUTO_ENDING_SOON = "_ending_soon"
_AUTO_RECOMMENDED = "_recommended"
_AUTO_SOURCES = frozenset({_AUTO_ENDING_SOON, _AUTO_RECOMMENDED})

#: Minimum prescreen score to earn a watchlist slot. Two, not one, and the difference
#: matters more here than anywhere else in the pipeline: "vintage" is the one positive
#: term shared by *every* vertical, and this is an estate auction house, so essentially
#: every lot on the site carries it. At a threshold of one, the quality gate was
#: satisfied by literally everything and did nothing at all — measured on the live
#: catalogue, 566 of 1,274 lots passed on "vintage" alone. Two forces a *discriminating*
#: signal: either a maker hit (worth two on its own) or two distinct positive terms.
#: The same measurement at two yields Tiffany & Co. sterling and Elsa Peretti rather
#: than boxed ornaments.
_MIN_SIGNAL = 2


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


def _probe_categories(client: EbthClient, out_dir: Path) -> int:
    """Answer one question — which parameter narrows /browse to a category — and nothing
    else.

    Two attempts at folding this into the full ``--probe`` both died on the job clock
    without publishing a byte: the full probe does a great deal of other work first, and
    a step-level timeout SIGKILLs the process, so no ``except`` clause and no ``finally``
    ever runs. Evidence that only survives a graceful exit is evidence you lose exactly
    when the run is in trouble.

    So this writes each trial to disk the moment it lands. A kill at any point leaves
    every completed trial on disk, which is all the question actually needs.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "category-probe.json"
    report: dict = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "question": "which query parameter narrows /browse to a single category?",
        "trials": [],
    }

    def flush() -> None:
        try:
            path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        except OSError as exc:
            log.warning("probe_write_failed", error=str(exc)[:120])

    flush()
    for label, url in client.category_filter_urls():
        trial = client.measure_query(url)
        trial["label"] = label
        base = report.get("baseline_total")
        total = trial.get("total_items")
        trial["narrowed"] = total is not None and base is not None and total < base
        if label == "baseline":
            report["baseline_total"] = total
        report["trials"].append(trial)
        flush()
        print(f"{label:34} total={total} narrowed={trial['narrowed']}"
              f"{' ERROR=' + trial['error'] if 'error' in trial else ''}", flush=True)

    winners = [t["label"] for t in report["trials"] if t.get("narrowed")]
    report["winners"] = winners
    flush()
    print(f"\nbaseline={report.get('baseline_total')}  winners={winners or 'NONE'}")
    return 0


def _probe(client: EbthClient, urls: list[str], out_dir: Path) -> int:
    # Whatever the probe managed to learn gets written even if it dies partway. The whole
    # report used to be built in memory and written once at the end, so when a run was
    # cancelled on the job timeout it published nothing at all — including the sections
    # that had completed in the first two minutes. A probe exists to produce evidence;
    # losing the evidence it already has is the one failure mode worth engineering out.
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "probe.json"

    def dump(data: dict) -> None:
        try:
            path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        except OSError as exc:
            log.warning("probe_write_failed", error=str(exc)[:120])

    try:
        report = client.probe(urls)
    except BaseException as exc:  # noqa: BLE001 — includes the cancellation we saw
        partial = getattr(client, "partial_probe", None)
        dump({"aborted": f"{type(exc).__name__}: {exc}"[:300], "partial": partial or {}})
        raise
    dump(report)
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


def _search_targets(raw: str, *, default_vertical: str) -> list[tuple[str, str]]:
    """(vertical key, search URL) pairs to run this pass.

    This used to fan out over EBTH's category tree, one query per category per sort, on
    the reasoning that asking the house what it filed under "Jewelry and Watches" beats
    guessing keywords. The reasoning still holds; the mechanism did not. Measured across
    a full run, all 14 category ids returned ``total_items=1986`` — the identical count
    the *unfiltered* ``days_left=2`` browse returns — so ``category_id`` is accepted and
    silently ignored on this route, and 42 of the 44 requests were byte-for-byte the same
    query. The whole run saw one page of one pool, fetched 44 times, out of the 1,986
    lots actually closing inside the window.

    So the fetch budget goes to *depth* instead: the same handful of genuinely distinct
    queries, paged out far enough to cover the window. ``days_left`` and ``sort`` both
    demonstrably work (1,211 / 1,986 / 3,169 lots for 1 / 2 / 3 days), which is what
    makes this worth doing — the server really will hand over the whole ending-soon
    ordering a page at a time.

    Set ``EBTH_CATEGORY_PARAM`` to restore per-category discovery the moment the real
    parameter name is known (``EbthClient._try_category_filters`` is looking for it);
    the category tree and its vertical mapping are kept intact for exactly that.

    ``EBTH_SEARCH_URLS`` still overrides everything with literal URLs, screened under
    ``default_vertical`` — the documented escape hatch, unchanged.
    """
    days = _int_env("EBTH_TIME_CRITICAL_DAYS") or 2
    if raw.strip():
        targets = [(default_vertical, u) for u in _search_urls(raw)]
        targets.append((_AUTO_ENDING_SOON,
                        f"{_EBTH_BASE}/browse?sort={SORTS['ending_soon']}&days_left={days}"))
        return targets

    sorts = [s.strip() for s in _env("EBTH_SORTS", "ending_soon,highest_bid,most_bids").split(",")
             if s.strip() in SORTS]
    targets: list[tuple[str, str]] = []

    # Measured, not assumed: category_slug narrows /browse (6,133 -> 2,271 for Jewelry
    # and Watches) while category_id — the name the site's own Categories filter block
    # publishes as its query parameter — leaves the count untouched. See
    # docs/auctions/category-probe.json.
    param = _env("EBTH_CATEGORY_PARAM", "category_slug").strip()
    if param:
        # Per-category discovery is strictly better than a site-wide sweep: the vertical
        # then comes from EBTH's own filing rather than from our classifier guessing at
        # the title. Set EBTH_CATEGORY_PARAM="" to fall back to the site-wide sweeps.
        for cat in resolve_categories(_env("EBTH_CATEGORIES", "")):
            value = str(cat.id) if param.endswith("_id") else cat.slug
            for sort_key in sorts:
                targets.append((
                    cat.vertical,
                    f"{_EBTH_BASE}/browse?{param}={value}"
                    f"&sort={SORTS[sort_key]}&days_left={days}",
                ))
    else:
        for sort_key in sorts:
            targets.append((_AUTO_ENDING_SOON,
                            f"{_EBTH_BASE}/browse?sort={SORTS[sort_key]}&days_left={days}"))

    targets.append((_AUTO_RECOMMENDED, f"{_EBTH_BASE}/browse?sort={SORTS['recommended']}"))
    # Distinct queries only. With category_id inert the category URLs collapse onto each
    # other, and a duplicate request costs a full browser navigation to learn nothing.
    seen: set[str] = set()
    return [(v, u) for v, u in targets if not (u in seen or seen.add(u))]


def _best_vertical(entry: acat.AuctionEntry) -> str:
    """Best-scoring vertical match across everything this pipeline knows how to price,
    for a lot discovered without a category (the site-wide ending-soon / recommended
    sweeps). Returns "" unless one vertical wins outright on a discriminating signal.
    Lots found *through* a category already carry that vertical and never reach this."""
    listing = entry.to_listing()
    scored = sorted(
        ((prescreen(listing, v, require_photo=False).score, v.key) for v in all_verticals()),
        reverse=True,
    )
    best_score, best_key = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    # A score that every vertical ties on is not a classification — it means the lot
    # matched only a universal term like "vintage". Demand a strict winner that also
    # clears the quality bar, otherwise we have not actually identified anything.
    if best_score < _MIN_SIGNAL or best_score == runner_up:
        return ""
    # "" rather than a least-bad guess. The old fallback was "furniture", which is how a
    # lot of Barbie dolls came to be filed as furniture and then valued against furniture
    # guidance — a wrong vertical yields a confidently wrong number, which is worse than
    # no number. A lot nothing can classify is a lot we have no business pricing.
    return best_key


def _venue_block(catalog: acat.AuctionCatalog, entry: acat.AuctionEntry,
                 now: datetime) -> str:
    """What the market is already saying about this exact lot, for the appraiser.

    The listing handed to the appraiser carries no price at all — deliberately, so a $12
    opening bid could not anchor the estimate. That was right for an opening bid and
    badly wrong at the endgame: twenty bids at $250 with minutes left is not noise, it is
    this object's clearing price, and withholding it left the model to invent a figure
    from nothing. It invented $1,200 for a painting whose artist realises $111-$401.

    Also included: the house's own results for the same maker. Those are the most direct
    comparable that exists for "what will the next one fetch here", and the catalogue has
    been recording them all along without ever showing them to the appraiser.
    """
    lines = [
        "This lot is selling AT AUCTION on Everything But The House (an estate auction "
        "house), and you are estimating what it will FETCH AT AUCTION — a realised "
        "hammer price, not a gallery or retail ask.",
    ]
    left = entry.hours_left(now)
    if left is not None:
        when = f"{left * 60:.0f} minutes" if left < 1 else f"{left:.0f} hours"
        lines.append(f"Closes in: {when}")
    bid = entry.current_bid_cents or 0
    if bid:
        lines.append(
            f"Bidding so far: ${bid / 100:,.0f} across {entry.bid_count or 0} bids."
        )
        if (entry.bid_count or 0) >= 8 and left is not None and left <= 24:
            lines.append(
                "That is competitive bidding close to the end, so it is strong evidence "
                "of this lot's market price. If your estimate is far above it, say "
                "explicitly in `reasoning` why the bidders are wrong."
            )

    comps = acat.same_maker_lots(catalog, entry)
    if comps:
        lines.append("")
        lines.append("This house's own results for the same maker:")
        for c in comps:
            if c.final_price_cents:
                lines.append(f"- SOLD ${c.final_price_cents / 100:,.0f} — {c.title[:90]}")
            else:
                lines.append(
                    f"- bidding at ${(c.current_bid_cents or 0) / 100:,.0f} "
                    f"({c.bid_count or 0} bids) — {c.title[:90]}"
                )
        lines.append(
            "Treat these as the primary comparable. A realised price here outranks any "
            "recollection of the wider market, and two lots by one maker should not "
            "receive wildly different estimates without a stated reason."
        )
    return "\n".join(lines)


def _still_promising(entry: acat.AuctionEntry, multiplier: float) -> bool:
    """Is an already-appraised lot worth a watchlist slot regardless of its keywords?

    Only if the *same* guidance the board displays says "bid". Anything looser lets the
    page contradict itself: a first pass here used a cheap value-beats-bid proxy, and it
    rescued lots the board was simultaneously labelling PASS with a negative margin,
    because the proxy ignored the fees, logistics and margin cushion that produce the
    stance. If the board won't recommend it, it isn't worth tracking.

    The point of the exemption is the mistitled sleeper this pipeline exists to catch —
    a genuinely good piece whose title satisfies no keyword rule. An appraisal is real
    evidence about such a lot and should outrank a heuristic about its wording. A PASS
    is equally real evidence, in the other direction.
    """
    if entry.appraisal is None:
        return False
    g = bidding.guide(entry, multiplier=multiplier)
    return g is not None and g.stance == "bid"


def _refresh_watchlist(catalog: acat.AuctionCatalog, default_vertical: str, cap: int,
                       *, decide_days: float = 2.0, now: datetime | None = None,
                       multiplier: float | None = None) -> int:
    """Promote quality lots to the watchlist, newest evidence first.

    The gate is a *positive* signal — maker or material keywords — not merely "has a
    photo and a price" like the Marketplace pre-screen keeps: every auction lot has
    both, so the lenient rule would watch the entire site.

    Remaining slots go to lots inside the decision window *first*, then by keyword score,
    then soonest-ending. Window-before-score is deliberate and was wrong the other way
    round: valuation only ever happens inside the window, so a strong-scoring lot that
    closes next week holds a slot it cannot use while a weaker one closing tonight — the
    only kind a valuation can still change a decision about — is left off. Observed live,
    that ordering left 31 of 51 watched lots parked outside the window and starved the
    appraisal queue of the lots that were actually actionable.

    Each entry is screened against *its own* vertical (whichever category's search
    surfaced it), not one global choice — a jewelry lot judged by furniture's keyword
    list (walnut, teak, maker names like Lane) would almost never pass. Lots from the
    site-wide sweeps carry no category, so one is classified for them first; a lot that
    matches no vertical at all is not watched, because there is no honest way to price it.

    Slots are shared across verticals rather than handed to whoever scores highest
    overall, and every eligible lot competes for every slot each run — incumbents
    included. Without the sharing, furniture wins on volume alone (its keyword list is
    the oldest and richest). Without re-running the whole allocation, the list simply
    saturates and whichever vertical qualified first keeps the board forever.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now + timedelta(days=decide_days)
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    if multiplier is None:
        multiplier = bidding.endgame_multiplier(acat.calibration_pairs(catalog))

    def signal(entry: acat.AuctionEntry) -> int:
        v = get_vertical(entry.vertical or default_vertical)
        r = prescreen(entry.to_listing(), v, require_photo=False)
        return r.score if r.keep else -1

    def actionable(entry: acat.AuctionEntry) -> int:
        """1 for lots a valuation could still act on, so they sort ahead of the rest."""
        return int(entry.ends_at is not None and entry.ends_at <= cutoff)

    # Legacy entries predating category discovery carry no vertical, so they price and
    # render under the default. Give them a real one now that we can classify.
    for entry in catalog.lots.values():
        if entry.watch and not entry.vertical and entry.state in ("live", "ending"):
            entry.vertical = _best_vertical(entry) or default_vertical

    # Every eligible lot competes for every slot, incumbent or not. Sharing only the
    # *free* room was not enough: the list saturates at the cap, room goes to zero, and
    # whichever vertical qualified first keeps the whole board forever. Observed live —
    # jewelry held 109 of 150 slots while 89 qualifying art lots, 28 of them inside the
    # decision window, had nowhere to go. Allocating the full list each run is what makes
    # the share a share rather than a race.
    by_vertical: dict[str, list[tuple[int, int, int, acat.AuctionEntry]]] = {}
    for entry in catalog.lots.values():
        if entry.state not in ("live", "ending"):
            continue
        if entry.vertical in _AUTO_SOURCES:
            # No category came with it, so give it one — and drop it if none fits.
            entry.vertical = _best_vertical(entry)
            if not entry.vertical:
                entry.watch = False
                continue
        v = get_vertical(entry.vertical or default_vertical)
        # Appraised lots we would still act on stay eligible whatever their keywords say:
        # that is the mistitled sleeper the pipeline exists to catch, and an appraisal is
        # real evidence where a keyword rule is only a guess about wording.
        paid = _still_promising(entry, multiplier)
        if signal(entry) < _MIN_SIGNAL and not paid:
            entry.watch = False
            continue
        by_vertical.setdefault(v.key, []).append(
            (int(paid), actionable(entry), signal(entry), entry)
        )

    # Within a vertical: work already paid for first (an appraisal is money spent and its
    # guidance is on the board), then lots the window can act on, then strongest signal,
    # then soonest close.
    for bucket in by_vertical.values():
        bucket.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3].ends_at or far_future))

    # Round-robin across verticals: each takes its best remaining lot in turn until the
    # cap runs out. A vertical with few candidates drops out of the rotation and its
    # unused share goes to the others, so this never wastes a slot to enforce a quota.
    chosen: list[acat.AuctionEntry] = []
    cursors = dict.fromkeys(by_vertical, 0)
    while len(chosen) < cap and cursors:
        for key in list(cursors):
            if len(chosen) >= cap:
                break
            bucket, i = by_vertical[key], cursors[key]
            if i >= len(bucket):
                del cursors[key]
                continue
            chosen.append(bucket[i][3])
            cursors[key] = i + 1

    keep = {id(e) for e in chosen}
    promoted = dropped = 0
    for bucket in by_vertical.values():
        for _paid, _act, _score, entry in bucket:
            if id(entry) in keep:
                if not entry.watch:
                    promoted += 1
                entry.watch = True
            else:
                if entry.watch:
                    dropped += 1
                entry.watch = False
    if dropped:
        log.info("watchlist_dropped", count=dropped)
    return promoted


def _make_client() -> EbthClient:
    """The default EBTH client for a real run — a browser fetcher unless EBTH_FETCH says
    otherwise. Split out as a seam so tests inject a fake without a browser."""
    return build_client()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Track EBTH auctions and publish bid guidance")
    ap.add_argument("--out", default="docs/auctions")
    ap.add_argument("--catalog", default="docs/auctions/catalog.json")
    ap.add_argument("--max-appraisals", type=int,
                    default=_int_env("MAX_AUCTION_APPRAISALS") or 20)
    ap.add_argument("--vertical", default=_env("VERTICAL", "furniture"))
    ap.add_argument("--probe-categories", action="store_true",
                    help="test which query parameter narrows /browse to one category, "
                         "writing each trial to disk as it lands")
    ap.add_argument("--probe", action="store_true",
                    help="fetch the configured pages and publish a structure report "
                         "instead of running the pipeline")
    ap.add_argument("--dry-run", action="store_true", help="no AI; track and render only")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    targets = _search_targets(_env("EBTH_SEARCH_URLS", ""), default_vertical=args.vertical)
    # A browser client owns a Chromium process; close it no matter how main() exits.
    client = _make_client()
    try:
        return _run(args, out_dir, targets, client)
    finally:
        client.close()


def _run(args, out_dir: Path, targets: list[tuple[str, str]], client: EbthClient) -> int:
    if args.probe_categories:
        return _probe_categories(client, out_dir)
    if args.probe:
        return _probe(client, [u for _v, u in targets], out_dir)

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

    default_vertical = get_vertical(args.vertical)
    now = datetime.now(timezone.utc)

    # 1. Fetch the search endpoint(s). This is BOTH discovery and snapshot: EBTH's search
    #    payload returns every matching lot with its live bid, count, and end time, so one
    #    page load refreshes the whole watchlist that's still in results and surfaces new
    #    lots at once. It runs every run — the endgame needs hourly bids, and a single
    #    headless page load per query is a rounding error to a site this size.
    #
    #    Each batch is tagged with the vertical whose query surfaced it, first-wins
    #    (an entry already tagged by an earlier, more specific search keeps that tag).
    searches_failed = 0
    seen_ids: set[str] = set()
    for vkey, url in targets:
        try:
            items = client.search(url, max_pages=_DISCOVERY_MAX_PAGES)
        except Exception as exc:  # noqa: BLE001 — one search must not sink the run
            searches_failed += 1
            log.warning("ebth_search_failed", url=url, error=str(exc)[:200])
            continue
        seen_ids.update(i.item_id for i in items)
        rep = acat.observe_auctions(catalog, items, now=now)
        # Tag after observe_auctions so brand-new lots (just created as entries) get
        # stamped too, not only ones already in the catalogue from a prior run.
        for item in items:
            entry = catalog.lots.get(item.item_id)
            if entry is not None and not entry.vertical:
                entry.vertical = vkey
        log.info("ebth_searched", url=url, vertical=vkey, items=len(items), new=rep.new,
                 snapshots=rep.snapshots,
                 # EBTH's own result count for this query. Identical totals across
                 # different category_id values would mean the category filter is being
                 # ignored and every vertical is being credited with the same generic
                 # first page — which one page of 96 items looks exactly like.
                 total=getattr(client, "last_total_items", None))
    if targets and searches_failed < len(targets):
        catalog.last_discovery_at = now
    scan_failed = bool(targets) and searches_failed == len(targets)

    # 2. Watchlist refresh from what the searches surfaced. The decision window is read
    #    here rather than at the appraisal step because promotion has to know it too —
    #    a watchlist slot is only worth spending on a lot the window can act on.
    decide_days = float(_env("EBTH_DECIDE_WITHIN_DAYS", "2"))
    promoted = _refresh_watchlist(
        catalog, args.vertical, _int_env("EBTH_MAX_WATCH") or 150,
        decide_days=decide_days, now=now,
    )
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

    # 3. Appraise the watchlist, once per lot, closest ending first — and only lots
    #    actually closing inside the decision window. A lot ending next week will have
    #    its bid move many times before any decision is due, so valuing it now buys
    #    nothing that valuing it in two days wouldn't, and valuation is the only
    #    expensive step in the run.
    appraised = 0
    failures: list[str] = []
    todo = acat.unappraised_watch(
        catalog, within_days=decide_days, now=now
    )[: args.max_appraisals]
    if todo and not args.dry_run:
        provider = get_appraiser(_env("APPRAISER_PROVIDER", "claude-code"))
        listings = [e.to_listing() for e in todo]
        photos = _download_photos(listings, out_dir / "_photos")
        photos_dir = out_dir / "photos"
        photos_dir.mkdir(parents=True, exist_ok=True)
        for entry in todo:
            listing = entry.to_listing()
            paths = photos.get(entry.id, [])
            entry_vertical = get_vertical(entry.vertical) if entry.vertical else default_vertical
            try:
                appraisal = provider.appraise(
                    listing, entry_vertical, image_paths=paths or None,
                    venue=_venue_block(catalog, entry, now),
                )
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

    # 5. Guidance and the page. Shipping is per-lot (a flat parcel rate for small
    #    things, a real round trip for furniture and rugs), so it isn't passed here —
    #    `guide` derives it from each lot's own vertical unless EBTH_SHIPPING_CENTS
    #    forces a single site-wide override.
    premium = float(_env("EBTH_PREMIUM_PCT", "0.15"))
    shipping_override = _int_env("EBTH_SHIPPING_CENTS")
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
            shipping_cents=shipping_override, now=now,
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
