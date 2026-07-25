"""Tier-1 triage: a cheap text-only filter run on every new listing.

Its job is coarse: is this plausibly a restoration-worthy solid-wood / vintage /
branded piece being underpriced, or is it flat-pack particleboard, a scam, or
irrelevant? Only ``promising=True`` listings advance to the expensive vision appraisal.
"""

from __future__ import annotations

from dealfinder.config import get_settings
from dealfinder.core.schemas import RawListing, TriageResult
from dealfinder.logging import get_logger
from dealfinder.valuation.client import get_client

log = get_logger(__name__)

_SYSTEM = (
    "You are a triage filter for a furniture-restoration reseller. Given a Facebook "
    "Marketplace listing's text (no photos), decide whether it is plausibly a "
    "restoration-worthy, potentially-underpriced piece worth a closer look. Favour "
    "solid wood, vintage/antique, mid-century, and known makers. Reject flat-pack / "
    "particleboard / IKEA-tier items, obvious scams, and non-furniture. Be inclusive at "
    "this stage — a cheap deeper look will follow — but filter out the clearly hopeless."
)


def _prompt(listing: RawListing) -> str:
    price = (
        f"${listing.asking_price_cents / 100:.0f}"
        if listing.asking_price_cents is not None
        else "unknown"
    )
    return (
        f"Title: {listing.title}\n"
        f"Asking price: {price}\n"
        f"Location: {listing.location_text}\n"
        f"Description:\n{listing.description[:2000]}"
    )


def triage_listing(listing: RawListing) -> TriageResult:
    """Run tier-1 triage synchronously for a single listing.

    For bulk runs prefer the Batch API (50% cost) — this per-item path is used by the
    Phase-1 pipeline and the dashboard 'scrape now' button.
    """
    client = get_client()
    model = get_settings().triage_model
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": _prompt(listing)}],
            output_format=TriageResult,
        )
        result = response.parsed_output
        if result is None:  # refusal or parse miss — fail open (keep the listing)
            return TriageResult(promising=True, reason="triage parse failed; kept")
        return result
    except Exception as exc:  # noqa: BLE001 — never let triage crash the pipeline
        log.warning("triage_failed", listing=listing.fb_listing_id, error=str(exc))
        return TriageResult(promising=True, reason=f"triage error; kept ({exc})")
