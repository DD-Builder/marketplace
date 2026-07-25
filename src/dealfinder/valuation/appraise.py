"""Tier-2 appraisal: Opus 4.8 vision valuation of a promising listing.

Sends the downloaded photos plus the description and asking price, and forces a
structured :class:`AppraisalResult` via ``messages.parse``. Adaptive thinking gives the
model room to reason about restoration economics.
"""

from __future__ import annotations

import base64
from pathlib import Path

from dealfinder.config import get_settings
from dealfinder.core.schemas import AppraisalResult
from dealfinder.logging import get_logger
from dealfinder.valuation.client import get_client
from dealfinder.valuation.comparables import Comp

log = get_logger(__name__)

_MAX_IMAGES = 6  # bound image-token cost per appraisal

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_SYSTEM = (
    "You are an expert appraiser advising a restoration reseller. From the photos and text "
    "of a marketplace listing, identify the piece (style, era, maker, materials), assess "
    "its condition and what restoration it needs, and estimate: its as-is value, its resale "
    "value once restored, the cost of materials/parts to restore it, and the hands-on hours "
    "of effort. Be realistic and conservative — these estimates drive real buying decisions. "
    "\n\n"
    "Value the restored piece at what it would REALISTICALLY sell for in a regional "
    "second-hand market (local marketplace, a good booth, eBay sold prices) — NOT at "
    "aspirational dealer listing prices. Sites like 1stDibs list museum-grade refurbishments "
    "at multiples of real resale; treat any such reference as a soft ceiling to discount "
    "heavily, never as the market comp. When unsure of the maker or model, say so and lower "
    "your confidence rather than assuming the optimistic identification. "
    "\n\n"
    "All monetary values are in US cents. confidence is 0.0-1.0. deal_score is your own "
    "0-100 gut read (the app computes its own authoritative score separately)."
)


def _image_block(path: Path) -> dict | None:
    media_type = _MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None or not path.exists():
        return None
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _comps_block(comps: list[Comp]) -> str:
    if not comps:
        return ""
    lines = [
        f"- {c.title}: ${c.sold_price_cents / 100:.0f} ({c.source})" for c in comps
    ]
    return "\n\nRecent comparable sales:\n" + "\n".join(lines)


def _image_url_block(url: str) -> dict:
    return {"type": "image", "source": {"type": "url", "url": url}}


def appraise(
    *,
    description: str,
    asking_price_cents: int | None,
    image_paths: list[Path] | None = None,
    image_urls: list[str] | None = None,
    comps: list[Comp] | None = None,
    guidance: str = "",
) -> tuple[AppraisalResult, int, int]:
    """Return (appraisal, input_tokens, output_tokens).

    Provide ``image_paths`` (local files, used by the live pipeline) and/or ``image_urls``
    (remote, used by the measurement pilot straight off the scraper's JSON). ``guidance`` is
    the active vertical's category-specific instruction, appended to the system prompt.
    """
    client = get_client()
    model = get_settings().appraise_model
    system = f"{_SYSTEM}\n\n{guidance}" if guidance else _SYSTEM

    content: list[dict] = []
    for p in (image_paths or [])[:_MAX_IMAGES]:
        block = _image_block(p)
        if block is not None:
            content.append(block)
    for u in (image_urls or [])[: _MAX_IMAGES - len(content)]:
        content.append(_image_url_block(u))

    price = (
        f"${asking_price_cents / 100:.0f}"
        if asking_price_cents is not None
        else "unknown"
    )
    text = (
        f"Asking price: {price}\n\n"
        f"Seller description:\n{description[:3000]}"
        f"{_comps_block(comps or [])}"
    )
    content.append({"type": "text", "text": text})

    # Effort defaults to "high" on Opus 4.8, so it isn't set explicitly here — passing
    # output_config alongside parse's output_format would collide. max_tokens is generous
    # because adaptive thinking spends from the same budget and would otherwise truncate
    # the structured output (finding B9).
    response = client.messages.parse(
        model=model,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": content}],
        output_format=AppraisalResult,
    )
    result = response.parsed_output
    if result is None:
        raise RuntimeError(
            f"appraisal returned no parsed output (stop_reason={response.stop_reason})"
        )
    return result, response.usage.input_tokens, response.usage.output_tokens
