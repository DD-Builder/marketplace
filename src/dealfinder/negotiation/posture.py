"""Map the negotiation posture slider to concrete prompt parameters.

Posture is a single scalar the UI exposes as a slider:
    0   = maximally aggressive / lowball ("I don't really need it")
    100 = maximally eager ("I'll pay asking today, I must have it")
Everything in between interpolates anchor aggressiveness, tone, and urgency.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PostureParams:
    label: str
    anchor_guidance: str
    tone: str
    urgency: str
    walkaway: str


def posture_params(posture: int) -> PostureParams:
    p = max(0, min(100, posture))
    if p <= 25:
        return PostureParams(
            label="aggressive",
            anchor_guidance=(
                "Open well below the asking price. Anchor to the item's as-is value and "
                "cite specific flaws and the cost/effort to restore it as justification."
            ),
            tone="detached and transactional; you have other options",
            urgency="none — imply you're in no hurry and comparison-shopping",
            walkaway="signal clear willingness to walk away if the price doesn't move",
        )
    if p <= 50:
        return PostureParams(
            label="measured",
            anchor_guidance=(
                "Open moderately below asking, grounded in the condition and comparable "
                "pricing. Leave room to meet somewhere in the middle."
            ),
            tone="friendly but businesslike",
            urgency="mild — you're interested but not desperate",
            walkaway="hint you have a budget ceiling without being confrontational",
        )
    if p <= 75:
        return PostureParams(
            label="keen",
            anchor_guidance=(
                "Open close to asking with a small, polite discount request. Emphasise "
                "that you're a serious, easy buyer."
            ),
            tone="warm and enthusiastic",
            urgency="fairly high — you'd like to arrange pickup soon",
            walkaway="none — focus on closing smoothly",
        )
    return PostureParams(
        label="eager",
        anchor_guidance=(
            "Offer at or very near the asking price. Prioritise locking in the deal over "
            "saving a few dollars."
        ),
        tone="warm, appreciative, and decisive",
        urgency="high — you want to commit and pick up today",
        walkaway="none — you must have this item",
    )
