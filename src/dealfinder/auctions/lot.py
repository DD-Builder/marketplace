"""A lot you are bidding on, described by you.

This replaces a record that used to be populated by harvesting an auction house's site.
The arithmetic in :mod:`dealfinder.auctions.bidding` was always lawful — it is your own
reasoning about your own ceiling — but it was coupled to that harvested store, so it went
when the store did. This is the same shape, filled in by hand from a lot you are looking
at, from a catalogue you were given, or from any feed you have the right to use.

No I/O, no persistence, no network. It is a value object and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from dealfinder.core.schemas import AppraisalResult, RawListing, RawPhoto

#: Hours before close that count as the endgame — the window where most of the money
#: arrives and the only one where a decision changes anything.
ENDGAME_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BidPoint:
    """One observation of the price, so velocity can be worked out over time."""

    at: datetime
    bid_cents: int | None = None
    bid_count: int | None = None


@dataclass
class Lot:
    """One auction lot under consideration."""

    id: str = ""
    title: str = ""
    description: str = ""
    #: Which pricing vertical governs it — drives the logistics assumption (a flat parcel
    #: rate for something shippable, a real round trip for something bulky).
    vertical: str = ""
    current_bid_cents: int | None = None
    bid_count: int | None = None
    ends_at: datetime | None = None
    #: Your own price observations, oldest first. Optional: without at least two points
    #: velocity is simply unavailable, which the bid math handles.
    bid_history: list[BidPoint] = field(default_factory=list)
    #: The valuation, once you have one. None means the lot cannot be advised on yet.
    appraisal: AppraisalResult | None = None
    photo_urls: list[str] = field(default_factory=list)

    def hours_left(self, now: datetime | None = None) -> float | None:
        if self.ends_at is None:
            return None
        return (self.ends_at - (now or _now())).total_seconds() / 3600

    def to_listing(self) -> RawListing:
        """Bridge into the shared appraisal machinery.

        ``asking_price_cents`` is deliberately None: a current bid is not a seller's
        estimate of value, and handing the appraiser a transient opening bid as "the
        asking price" would anchor the valuation to noise.
        """
        return RawListing(
            listing_id=self.id,
            title=self.title,
            description=self.description,
            asking_price_cents=None,
            photos=[RawPhoto(remote_url=u, position=i)
                    for i, u in enumerate(self.photo_urls)],
        )
