"""Draft a negotiation reply for one listing and publish it next to the board.

The board is a static page, so it can't run a model itself. Instead the page dispatches
this through GitHub Actions with your posture and the conversation so far; the job writes
``.drafts/<listing-id>.json`` and commits it, and the page — which is polling that
file — shows the drafts when they land.

That indirection is what keeps the whole thing free: the model call happens on GitHub's
compute, billed to your Claude subscription, with no server and no API key in a browser.

Nothing is ever sent to a seller. You read the drafts, edit whichever you like, and send
it yourself from Messenger.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dealfinder import catalog as catalog_mod
from dealfinder.logging import get_logger
from dealfinder.negotiation.drafts import draft_replies, get_drafter, offers_above
from dealfinder.negotiation.posture import posture_params

log = get_logger(__name__)


#: The profit that has to be left on the table for a flip to be worth doing at all:
#: at least $150, or 20% of the restored value for bigger pieces.
_MIN_MARGIN_CENTS = 15000
_MIN_MARGIN_PCT = 0.20


def _walkaway_cents(entry, hourly_rate_cents: int) -> int | None:
    """The most you should pay: what the piece is worth restored, less restoration cost,
    less your labour at your rate, less the margin that makes the flip worth doing.

    Derived from the stored appraisal, so it costs nothing to compute. Never above the
    asking price — the previous formula (ask + margin/2) literally told the drafting
    model "the most I will pay is $550" about a $250 listing, and because offers_above()
    compared drafts against the same inflated number, the one guard built to catch an
    overpaying draft could never fire.
    """
    if entry is None or entry.appraisal is None:
        return None
    a = entry.appraisal
    required_margin = max(
        _MIN_MARGIN_CENTS, int(a.est_restored_resale_value_cents * _MIN_MARGIN_PCT)
    )
    labour = int(a.est_restoration_effort_hours * hourly_rate_cents)
    walkaway = (
        a.est_restored_resale_value_cents
        - a.est_restoration_cost_cents
        - labour
        - required_margin
    )
    ask = entry.asking_price_cents
    if ask:
        walkaway = min(walkaway, ask)  # you can always simply pay the asking price
    return walkaway if walkaway > 0 else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draft negotiation replies for one listing")
    ap.add_argument("--listing-id", required=True)
    ap.add_argument("--posture", type=int, default=40,
                    help="0 = lowball and ready to walk, 100 = pay asking today")
    ap.add_argument("--conversation", default="", help="the thread so far, most recent last")
    ap.add_argument("--notes", default="", help="condition flaws or other leverage")
    ap.add_argument("--catalog", default="docs/catalog.json")
    # Accepted for workflow compatibility; the walk-away derives from the appraisal alone.
    ap.add_argument("--pieces", default="docs/pieces.json", help=argparse.SUPPRESS)
    ap.add_argument("--out", default=".drafts",
                    help="where the drafts JSON is written. Deliberately outside docs/: "
                         "the page reads drafts through the Contents API, and pasted "
                         "seller conversations must not be published as web pages")
    ap.add_argument("--provider", default=os.getenv("APPRAISER_PROVIDER") or "claude-code")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{args.listing_id}.json"

    try:
        catalog = catalog_mod.load_catalog(Path(args.catalog))
    except catalog_mod.CatalogCorrupt as exc:
        _write(dest, {"listing_id": args.listing_id, "status": "error",
                      "error": str(exc)[:400]})
        print(str(exc), file=sys.stderr)
        return 6
    entry = catalog.listings.get(args.listing_id)
    if entry is None:
        # Still write a result — a page polling for this file would otherwise hang forever
        # with no explanation.
        _write(dest, {
            "listing_id": args.listing_id, "status": "error",
            "error": "That listing isn't in the catalogue. Run the board first.",
        })
        print(f"unknown listing {args.listing_id}", file=sys.stderr)
        return 2

    try:
        hourly = int((os.getenv("HOURLY_RATE_CENTS") or "").strip() or 3000)
    except ValueError:
        hourly = 3000
    walkaway = _walkaway_cents(entry, hourly)
    notes = args.notes or (entry.appraisal.condition_assessment if entry.appraisal else "")

    try:
        drafts = draft_replies(
            posture=args.posture,
            listing_title=entry.title or "this piece",
            asking_price_cents=entry.asking_price_cents,
            walkaway_price_cents=walkaway,
            condition_notes=notes,
            conversation=args.conversation,
            drafter=get_drafter(args.provider),
        )
    except Exception as exc:  # noqa: BLE001 — the page must see *why*, not just silence
        log.warning("draft_failed", listing=args.listing_id, error=str(exc)[:300])
        _write(dest, {
            "listing_id": args.listing_id, "status": "error", "error": str(exc)[:400],
        })
        print(f"draft generation failed: {exc}", file=sys.stderr)
        return 1

    payload = {
        "listing_id": args.listing_id,
        "status": "ok",
        "title": entry.title,
        "asking_price_cents": entry.asking_price_cents,
        "walkaway_price_cents": walkaway,
        "posture": args.posture,
        "posture_label": posture_params(args.posture).label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "drafts": [
            {
                "text": d.text,
                "rationale": d.rationale,
                # Surfaced, not suppressed: the model may be quoting the seller's number
                # back at them, and only you can tell the difference.
                "over_walkaway_cents": offers_above(d.text, walkaway),
            }
            for d in drafts.drafts
        ],
    }
    _write(dest, payload)
    print(f"wrote {len(payload['drafts'])} drafts to {dest}")
    return 0


def _write(dest: Path, payload: dict) -> None:
    # Every payload — errors included — carries generated_at. The page distinguishes a
    # fresh result from a stale one by this stamp; an error payload without it made a
    # repeated failure indistinguishable from no answer, and the page polled out its
    # full five minutes before giving up.
    payload.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
