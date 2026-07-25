"""Draft a negotiation reply for one listing and publish it next to the board.

The board is a static page, so it can't run a model itself. Instead the page dispatches
this through GitHub Actions with your posture and the conversation so far; the job writes
``docs/drafts/<listing-id>.json`` and commits it, and the page — which is polling that
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
from dealfinder.engine import evaluate_piece
from dealfinder.logging import get_logger
from dealfinder.negotiation.drafts import draft_replies, get_drafter, offers_above
from dealfinder.negotiation.posture import posture_params
from dealfinder.pieces import costs_by_id, load_ledger

log = get_logger(__name__)


def _walkaway_cents(entry, hourly_rate_cents: int, logged_costs) -> int | None:
    """The most you should pay: what the piece is worth restored, less restoration and the
    margin you need. Derived from the stored appraisal, so it costs nothing to compute."""
    if entry is None or entry.appraisal is None:
        return None
    piece = evaluate_piece(
        entry.to_listing(), entry.appraisal,
        hourly_rate_cents=hourly_rate_cents, logged_costs=logged_costs,
    )
    ask = entry.asking_price_cents or 0
    # Your margin at the asking price, halved: the point below which haggling stops paying.
    return max(0, ask + piece.cash_margin_cents // 2) or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draft negotiation replies for one listing")
    ap.add_argument("--listing-id", required=True)
    ap.add_argument("--posture", type=int, default=40,
                    help="0 = lowball and ready to walk, 100 = pay asking today")
    ap.add_argument("--conversation", default="", help="the thread so far, most recent last")
    ap.add_argument("--notes", default="", help="condition flaws or other leverage")
    ap.add_argument("--catalog", default="docs/catalog.json")
    ap.add_argument("--pieces", default="docs/pieces.json")
    ap.add_argument("--out", default="docs/drafts", help="where the drafts JSON is written")
    ap.add_argument("--provider", default=os.getenv("APPRAISER_PROVIDER") or "claude-code")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{args.listing_id}.json"

    catalog = catalog_mod.load_catalog(Path(args.catalog))
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

    hourly = int((os.getenv("HOURLY_RATE_CENTS") or "3000").strip() or 3000)
    logged = costs_by_id(load_ledger(Path(args.pieces))).get(args.listing_id)
    walkaway = _walkaway_cents(entry, hourly, logged)
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
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=1, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
