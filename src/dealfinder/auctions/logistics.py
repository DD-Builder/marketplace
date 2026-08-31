"""What it actually costs to take possession of a won lot.

The Marketplace side of this project assumes you restore what you buy, so its economics
are dominated by materials and bench hours. The auction side assumes the opposite: you
are buying finished pieces to resell as they are. That makes *getting the thing home*
the real cost line, and it is not a rounding error — a $35 parcel and a 166-mile round
trip to Cincinnati differ by enough to flip a marginal lot from "bid" to "pass".

Two regimes, decided by the vertical's ``bulky`` flag:

* **Shippable** (jewelry, watches, art, decor, most collectibles) — a flat parcel rate.
  Small, insurable, and the house ships it.
* **Bulky** (furniture, rugs) — you drive. The cost is the round trip from Lexington to
  Cincinnati: mileage at the IRS rate *plus* the hours at your own rate, because a
  half-day round trip is a half-day you did not spend otherwise.

Every figure is overridable from the environment, because fuel prices, your rate, and
the auction house's location are exactly the things that change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dealfinder.verticals import Vertical, get_vertical

#: Flat parcel cost for a small, shippable lot. Covers packing + insured ground for the
#: jewelry/watch/art/decor range; deliberately a single number rather than a weight
#: model, since the auction house quotes shipping per lot anyway and this only needs to
#: be right enough to keep a marginal bid honest.
DEFAULT_SHIP_CENTS = 3500

#: Lexington, KY → Cincinnati, OH, one way. EBTH's operation is Cincinnati-based, so a
#: bulky win is this drive twice.
DEFAULT_ONE_WAY_MILES = 83.0
DEFAULT_ONE_WAY_HOURS = 1.45

#: IRS standard mileage rate (cents/mile) — fuel, wear, and depreciation in one number,
#: which is the honest cost of putting a van on the road rather than just fuel.
DEFAULT_MILEAGE_RATE_CENTS = 70


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Logistics:
    """The cost of collecting one lot, and the story behind the number."""

    cost_cents: int
    #: True when this is a drive rather than a parcel — the board says which, because
    #: "you have to go get this one" changes whether a thin margin is worth it.
    pickup: bool
    detail: str

    @property
    def label(self) -> str:
        return "Pickup drive" if self.pickup else "Shipping"


def acquisition_cost(
    vertical: Vertical | str | None,
    *,
    hourly_rate_cents: int = 3000,
) -> Logistics:
    """What it costs to get this lot home, given its category."""
    v = vertical if isinstance(vertical, Vertical) else get_vertical(vertical or "")

    if not v.bulky:
        cents = _env_int("EBTH_SHIP_CENTS", DEFAULT_SHIP_CENTS)
        return Logistics(
            cost_cents=cents,
            pickup=False,
            detail=f"${cents / 100:,.0f} flat shipping (small, shippable lot)",
        )

    miles = _env_float("PICKUP_ONE_WAY_MILES", DEFAULT_ONE_WAY_MILES) * 2
    hours = _env_float("PICKUP_ONE_WAY_HOURS", DEFAULT_ONE_WAY_HOURS) * 2
    rate = _env_int("MILEAGE_RATE_CENTS", DEFAULT_MILEAGE_RATE_CENTS)
    drive_cents = round(miles * rate)
    time_cents = round(hours * hourly_rate_cents)
    total = drive_cents + time_cents
    return Logistics(
        cost_cents=total,
        pickup=True,
        detail=(
            f"${total / 100:,.0f} pickup — {miles:.0f} mi round trip "
            f"(${drive_cents / 100:,.0f}) plus {hours:.1f}h of your time "
            f"(${time_cents / 100:,.0f})"
        ),
    )
