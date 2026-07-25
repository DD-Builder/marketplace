"""Generate candidate reply messages for the human-in-the-loop negotiation panel.

Separates *draft generation* from *dispatch* (which does not exist yet) — the exact seam
a future auto-send feature would plug into. Nothing is ever sent from the app.
"""

from __future__ import annotations

from dealfinder.config import get_settings
from dealfinder.core.schemas import NegotiationDrafts
from dealfinder.logging import get_logger
from dealfinder.negotiation.posture import posture_params
from dealfinder.valuation.client import get_client

log = get_logger(__name__)

_SYSTEM = (
    "You are helping a furniture-restoration reseller negotiate with a private seller "
    "over Facebook Marketplace messenger. Write natural, concise, human messages — never "
    "robotic or template-like. Use only the leverage you're given (real condition flaws, "
    "the buyer's walk-away price). Produce 1-3 short candidate replies the buyer can pick "
    "from, each with a one-line rationale."
)


def _dollars(cents: int | None) -> str:
    return f"${cents / 100:.0f}" if cents is not None else "unknown"


def draft_replies(
    *,
    posture: int,
    target_price_cents: int | None,
    asking_price_cents: int | None,
    listing_title: str,
    condition_notes: str,
    conversation: str,
) -> NegotiationDrafts:
    """Return candidate reply drafts tuned to the posture."""
    params = posture_params(posture)
    prompt = (
        f"Item: {listing_title}\n"
        f"Asking price: {_dollars(asking_price_cents)}\n"
        f"My walk-away (max I'll pay): {_dollars(target_price_cents)}\n"
        f"Known condition / flaws to use as leverage: {condition_notes or 'none noted'}\n\n"
        f"Negotiation posture: {params.label}\n"
        f"- Anchoring: {params.anchor_guidance}\n"
        f"- Tone: {params.tone}\n"
        f"- Urgency: {params.urgency}\n"
        f"- Walk-away signalling: {params.walkaway}\n\n"
        f"Conversation so far (most recent last):\n{conversation.strip() or '(no messages yet — write an opener)'}\n\n"
        "Write my next message."
    )
    client = get_client()
    model = get_settings().negotiation_model
    response = client.messages.parse(
        model=model,
        max_tokens=1500,
        thinking={"type": "adaptive"},
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=NegotiationDrafts,
    )
    drafts = response.parsed_output
    if drafts is None or not drafts.drafts:
        raise RuntimeError(
            f"draft generation returned nothing (stop_reason={response.stop_reason})"
        )
    return drafts
