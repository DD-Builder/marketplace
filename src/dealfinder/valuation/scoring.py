"""Deterministic deal-score computation.

The model returns its own ``deal_score``, but the *authoritative* score is computed
here from the structured numeric fields so it is transparent and tunable independently
of prompt drift. Both are stored; the feed sorts by the computed one.
"""

from __future__ import annotations

from dealfinder.core.schemas import AppraisalResult


def compute_deal_score(
    appraisal: AppraisalResult,
    asking_price_cents: int | None,
    hourly_rate_cents: int,
) -> float:
    """Return a 0-100 deal score.

    net_margin = restored_resale - asking - restoration_cost - (effort_hours * hourly_rate)

    The net margin is scaled to 0-100 and multiplied by confidence. A negative margin
    yields a low score. Scaling: $500 net margin -> ~50, saturating toward 100 by ~$1500.
    """
    if asking_price_cents is None:
        return 0.0

    effort_penalty = appraisal.est_restoration_effort_hours * hourly_rate_cents
    net_margin_cents = (
        appraisal.est_restored_resale_value_cents
        - asking_price_cents
        - appraisal.est_restoration_cost_cents
        - effort_penalty
    )

    if net_margin_cents <= 0:
        base = 0.0
    else:
        # Diminishing-returns curve: 100 * margin / (margin + $1000).
        margin_dollars = net_margin_cents / 100.0
        base = 100.0 * margin_dollars / (margin_dollars + 1000.0)

    score = base * appraisal.confidence
    return round(max(0.0, min(100.0, score)), 2)
