"""Measurement pilot: run the real deal-finding funnel over a scraped JSON export.

Point this at the JSON an Apify Facebook Marketplace actor produces (or any list of
listing dicts). It runs the exact funnel the product would — pre-screen -> triage ->
Opus vision appraisal -> deal score — over *real* listings, prints the conversion at
every stage, and writes a CSV of the flagged deals for you to verify by hand.

That verification is the whole point: it turns "how many meaningful deals per 1,000?"
from a guess into your market's measured number, which you drop straight into the
economics calculator. It also validates the valuation engine on real data.

Usage:
  python pilot/run_pilot.py listings.json                 # full funnel (spends on AI)
  python pilot/run_pilot.py listings.json --dry-run       # scrape+pre-screen only, no AI
  python pilot/run_pilot.py listings.json --appraise-limit 40   # cap Opus calls (cost)
  python pilot/run_pilot.py listings.json --out results.csv

Field mapping is tolerant of common Apify actor schemas; override with --map if needed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# Allow running as a plain script without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dealfinder.core.schemas import RawListing, RawPhoto  # noqa: E402
from dealfinder.prescreen import prescreen  # noqa: E402

# --- Adapter for the Facebook Marketplace actor schema --------------------
# Verified against a real 91-listing export from apify/facebook-marketplace-scraper.
# Real records nest almost everything: title is a flat string, but price, description
# and location are objects, and photos are doubly nested ({image: {uri}}). We still
# fall back to a few common aliases so a different actor doesn't break the pilot.

_TITLE_KEYS = ["listingTitle", "title", "name", "marketplaceListingTitle"]
_PRICE_KEYS = ["listingPrice", "price", "amount", "priceAmount"]
_DESC_KEYS = ["description", "redactedDescription", "desc"]
_LOC_KEYS = ["locationText", "location", "city", "locationName"]
_URL_KEYS = ["itemUrl", "listingUrl", "url", "link", "facebookUrl"]
_ID_KEYS = ["id", "listingId", "itemId", "fbid"]
_IMG_KEYS = ["listingPhotos", "images", "photos", "imageUrls", "primaryPhotoUrls"]


def _first(rec: dict, keys: list[str]):
    for k in keys:
        if k in rec and rec[k] not in (None, "", []):
            return rec[k]
    return None


def _text(v) -> str:
    """Flatten a field that may be a bare string or a {'text': ...} object."""
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("text") or v.get("label") or "")
    return str(v)


def _to_cents(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, dict):
        # This actor gives the value already in minor units (cents) here.
        raw = v.get("amount_with_offset_in_currency") or v.get("amount_with_offset")
        if raw:
            try:
                return int(str(raw))
            except ValueError:
                pass
        v = v.get("amount")  # e.g. "80.00" (dollars)
    try:
        return int(round(float(str(v).replace("$", "").replace(",", "")) * 100))
    except (TypeError, ValueError):
        return None


def _images(rec: dict) -> list[str]:
    val = _first(rec, _IMG_KEYS) or []
    urls: list[str] = []
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                # listingPhotos => {"image": {"uri": ...}}; other actors flatten it.
                img = item.get("image") if isinstance(item.get("image"), dict) else item
                u = img.get("uri") or img.get("url") or img.get("src")
                if u:
                    urls.append(u)
    # Fall back to the primary thumbnail if the gallery field was empty.
    if not urls:
        prim = rec.get("primaryListingPhoto") or {}
        u = prim.get("photo_image_url") or prim.get("uri")
        if u:
            urls.append(u)
    return urls


def to_raw(rec: dict, idx: int) -> RawListing:
    fb_id = str(_first(rec, _ID_KEYS) or f"row-{idx}")
    imgs = _images(rec)
    return RawListing(
        fb_listing_id=fb_id,
        title=_text(_first(rec, _TITLE_KEYS)),
        description=_text(_first(rec, _DESC_KEYS)),
        asking_price_cents=_to_cents(_first(rec, _PRICE_KEYS)),
        location_text=_text(_first(rec, _LOC_KEYS)),
        url=_text(_first(rec, _URL_KEYS)),
        photos=[RawPhoto(remote_url=u, position=i) for i, u in enumerate(imgs)],
        raw_json=rec,
    )


# --- Funnel ---------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Deal-finder measurement pilot")
    ap.add_argument("json_file", help="Apify (or any) JSON export: a list of listing dicts")
    ap.add_argument("--dry-run", action="store_true", help="pre-screen only, no AI spend")
    ap.add_argument("--triage-limit", type=int, default=400, help="max listings to triage")
    ap.add_argument("--appraise-limit", type=int, default=40, help="max Opus appraisals")
    ap.add_argument("--out", default="pilot/pilot_results.csv")
    args = ap.parse_args()

    records = json.loads(Path(args.json_file).read_text())
    if isinstance(records, dict):  # some exports wrap in {"items": [...]}
        records = records.get("items") or records.get("results") or [records]
    raws = [to_raw(r, i) for i, r in enumerate(records)]

    kept = [(r, prescreen(r)) for r in raws]
    survivors = [r for r, ps in kept if ps.keep]

    print(f"\n=== FUNNEL over {len(raws)} scraped listings ===")
    print(f"  pre-screen survivors : {len(survivors)}  ({_pct(len(survivors), len(raws))})")

    if args.dry_run:
        _write_csv(args.out, [(r, prescreen(r), None, None) for r in survivors])
        print(f"\nDry run — no AI. Wrote {args.out}. Re-run without --dry-run to triage + appraise.")
        return

    # Lazy AI imports so --dry-run needs no API key.
    from dealfinder.valuation import appraise as appraise_mod
    from dealfinder.valuation import scoring, triage
    from dealfinder.config import get_settings

    hourly = get_settings().hourly_rate_cents
    to_triage = survivors[: args.triage_limit]
    promising: list[RawListing] = []
    for r in to_triage:
        if triage.triage_listing(r).promising:
            promising.append(r)
    print(f"  triage promising     : {len(promising)}  ({_pct(len(promising), len(to_triage))} of triaged)")

    rows = []
    appraised = 0
    for r in promising:
        if appraised >= args.appraise_limit:
            break
        try:
            appraisal, _, _ = appraise_mod.appraise(
                description=r.description,
                asking_price_cents=r.asking_price_cents,
                image_urls=[p.remote_url for p in r.photos],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    ! appraisal failed for {r.fb_listing_id}: {exc}")
            continue
        appraised += 1
        score = scoring.compute_deal_score(appraisal, r.asking_price_cents, hourly)
        rows.append((r, prescreen(r), appraisal, score))

    strong = [x for x in rows if (x[3] or 0) >= 50]
    print(f"  appraised            : {appraised}")
    print(f"  strong deals (>=50)  : {len(strong)}")
    print()
    print("=== YOUR MARKET'S NUMBERS (per 1,000 scraped) ===")
    per_k = 1000 / len(raws) if raws else 0
    print(f"  pre-screen survivors : {len(survivors) * per_k:6.1f}")
    print(f"  AI-flagged deals     : {len(strong) * per_k:6.1f}   <-- feed this into the calculator")
    print("  (then open the CSV and mark which flagged deals are REAL to get your true base rate)")

    _write_csv(args.out, rows)
    print(f"\nWrote {args.out} — open it, sort by deal_score, and fill the 'your_verdict' column.")


def _pct(a: int, b: int) -> str:
    return f"{(100*a/b):.1f}%" if b else "0%"


def _write_csv(path: str, rows) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "id", "title", "price_$", "url", "prescreen_keep", "prescreen_reasons",
            "identified_item", "est_restored_$", "est_cost_$", "effort_h",
            "confidence", "deal_score", "reasoning", "your_verdict(real?)",
        ])
        for r, ps, appraisal, score in rows:
            price = f"{r.asking_price_cents/100:.0f}" if r.asking_price_cents else ""
            if appraisal is None:
                w.writerow([r.fb_listing_id, r.title, price, r.url, ps.keep,
                            "; ".join(ps.reasons), "", "", "", "", "", "", "", ""])
            else:
                w.writerow([
                    r.fb_listing_id, r.title, price, r.url, ps.keep, "; ".join(ps.reasons),
                    appraisal.identified_item,
                    f"{appraisal.est_restored_resale_value_cents/100:.0f}",
                    f"{appraisal.est_restoration_cost_cents/100:.0f}",
                    appraisal.est_restoration_effort_hours,
                    f"{appraisal.confidence:.2f}", f"{score:.0f}",
                    appraisal.reasoning, "",
                ])


if __name__ == "__main__":
    main()
