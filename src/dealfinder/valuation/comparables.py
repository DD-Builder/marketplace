"""Pricing-comparables seam.

MVP ships a no-op stub: Opus's internal estimate stands alone. Later phases plug in
eBay sold-listings, our own ``price_history``, or a curated comps table by implementing
``PricingComparableSource`` and feeding the results into the appraisal prompt.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class Comp(BaseModel):
    source: str
    title: str
    sold_price_cents: int
    url: str | None = None


class PricingComparableSource(Protocol):
    def get_comps(self, item_descriptor: str) -> list[Comp]:  # pragma: no cover
        ...


class NoopComparables:
    """Default source: returns nothing, so the model estimates unaided."""

    def get_comps(self, item_descriptor: str) -> list[Comp]:
        return []


def default_source() -> PricingComparableSource:
    return NoopComparables()
