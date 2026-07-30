"""Sanity bounds on the model's restoration estimates.

``est_restoration_cost_cents`` and ``est_restoration_effort_hours`` are two unconstrained
numbers a language model invents from a photograph, and they feed the deal score directly:
the cost is subtracted from margin, and the hours are multiplied by your rate and
subtracted again. A hallucinated "40 hours" quietly turns a good piece into a rejected one,
and a hallucinated "$5 and one hour" does the reverse.

The bounds come from published US refinishing-cost surveys (Fixr, Angi, HomeAdvisor,
Thumbtack), which agree closely:

* a typical full refinish runs **$341-$931**, averaging about **$631**
* heavy stripping (layered paint, deep wear) adds **$100-$500**

Every bound here is **absolute** — a fixed band, or a comparison against the piece's own
restored value. None of them depends on your hourly rate, and that is deliberate.

An earlier version also enforced "materials must not exceed the labour value", reasoning
from the surveys' finding that labour is ~85% of a refinishing job. That rule was wrong
twice over. The 85% figure describes a *professional's billed invoice* at $60-100/h, and
comparing it against the reseller's own $30/h opportunity cost is a unit mismatch — but
worse, materials genuinely can dominate: a replacement marble top or a yard of upholstery
fabric is real money against two hours of work. The rule fired hardest on exactly those
parts-heavy jobs, cutting a truthful $400 top down to $45 and inflating the margin 2x on
the pieces most likely to lose money. Cost is subtracted from margin, so a clamp that
lowers it is optimistic, and an optimistic clamp is the one thing this module must not be.

What remains only catches estimates that are absurd on their face, and reports what it
changed rather than silently rewriting the appraisal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Hours: below this nothing meaningful was done; above it you are rebuilding, not restoring.
MIN_HOURS = 0.5
MAX_HOURS = 40.0

#: Materials, in cents. The floor is a can of finish and sandpaper; the ceiling is generous
#: for hardware, veneer patches and upholstery on a large piece.
MIN_MATERIALS_CENTS = 500
MAX_MATERIALS_CENTS = 120000


@dataclass
class RestorationBounds:
    cost_cents: int
    effort_hours: float
    adjustments: list[str] = field(default_factory=list)

    @property
    def adjusted(self) -> bool:
        return bool(self.adjustments)


def clamp_restoration(
    cost_cents: int,
    effort_hours: float,
    *,
    restored_value_cents: int | None = None,
) -> RestorationBounds:
    """Bring an appraisal's restoration estimate inside published reality.

    Returns the clamped values plus a plain-language note for each change, so the board
    can show that a number was corrected instead of pretending the model said it.

    Every note describes a change that actually happened, and states the value the field
    really ended up with — a note is only recorded once the new value is final, so the
    two can't drift apart. Running this on its own output is a no-op that produces no
    notes, which is what lets the board re-derive the correction on every render.
    """
    notes: list[str] = []
    hours = float(effort_hours)
    cost = int(cost_cents)

    if hours < MIN_HOURS:
        notes.append(f"effort raised from {hours:g}h to the {MIN_HOURS:g}h minimum")
        hours = MIN_HOURS
    elif hours > MAX_HOURS:
        notes.append(f"effort capped from {hours:g}h to {MAX_HOURS:g}h")
        hours = MAX_HOURS

    if cost < MIN_MATERIALS_CENTS:
        notes.append(f"materials raised from ${cost / 100:.0f} to ${MIN_MATERIALS_CENTS / 100:.0f}")
        cost = MIN_MATERIALS_CENTS
    elif cost > MAX_MATERIALS_CENTS:
        notes.append(f"materials capped from ${cost / 100:.0f} to ${MAX_MATERIALS_CENTS / 100:.0f}")
        cost = MAX_MATERIALS_CENTS

    # Restoring a piece for more than it will ever be worth is not a restoration estimate.
    # The floor still wins, so the limit is computed first and the note quotes the value
    # actually assigned rather than the raw restored value.
    if restored_value_cents:
        limit = max(MIN_MATERIALS_CENTS, restored_value_cents)
        if cost > limit:
            notes.append(
                f"materials cut from ${cost / 100:.0f} to ${limit / 100:.0f} — no one "
                "spends more restoring a piece than it fetches finished"
            )
            cost = limit

    return RestorationBounds(cost_cents=cost, effort_hours=hours, adjustments=notes)
