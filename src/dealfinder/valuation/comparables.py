"""Pricing-comparables seam.

The default source returns nothing, so the model estimates unaided — which is how this
shipped originally. :mod:`dealfinder.sources.ebay` implements a real one against eBay's
free Browse API.

``Comp.is_sold`` matters more than it looks. eBay's Browse API returns *asking* prices,
and an asking price is what a hopeful seller wants, not what anyone paid. Anchoring a
restored-value estimate to unsold asks inflates it, so the flag is carried through to the
prompt and the model is told which kind it is looking at.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class Comp(BaseModel):
    source: str
    title: str
    price_cents: int
    #: True only for a realised sale. False means "someone is asking this" — weaker
    #: evidence, and systematically optimistic.
    is_sold: bool = False
    condition: str = ""
    url: str | None = None
    observed_at: datetime | None = None


class PricingComparableSource(Protocol):
    def get_comps(self, item_descriptor: str) -> list[Comp]:  # pragma: no cover
        ...


class NoopComparables:
    """Default source: returns nothing, so the model estimates unaided."""

    name = "none"

    def get_comps(self, item_descriptor: str) -> list[Comp]:
        return []


def default_source() -> PricingComparableSource:
    """The configured comps source: eBay when credentials exist, otherwise nothing."""
    from dealfinder.sources.ebay import from_env

    return from_env() or NoopComparables()
