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
* **labour is roughly 85% of the total** — materials are the small part

That last figure is the useful invariant. It means the two fields are not independent: a
plausible pairing has materials well below the labour value, and an estimate that violates
it badly is a sign the model has guessed rather than judged. This clamps the obvious
nonsense and reports what it changed, rather than silently rewriting the appraisal.
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

#: Published surveys put labour at ~85% of a refinishing job's cost, so materials should
#: not dwarf the labour value. Allowed to reach parity before we call it implausible.
MATERIALS_TO_LABOUR_CEILING = 1.0


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
    hourly_rate_cents: int = 3000,
    restored_value_cents: int | None = None,
) -> RestorationBounds:
    """Bring an appraisal's restoration estimate inside published reality.

    Returns the clamped values plus a plain-language note for each change, so the board
    can show that a number was corrected instead of pretending the model said it.
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

    # Labour dominates a real refinishing job. Materials far above the labour value means
    # the model has conflated "what the piece is worth" with "what fixing it costs".
    labour_cents = int(hours * hourly_rate_cents)
    ceiling = int(labour_cents * MATERIALS_TO_LABOUR_CEILING)
    if labour_cents > 0 and cost > ceiling:
        notes.append(
            f"materials cut from ${cost / 100:.0f} to ${ceiling / 100:.0f} — labour is "
            "about 85% of a real refinish, so materials shouldn't exceed the labour value"
        )
        cost = max(MIN_MATERIALS_CENTS, ceiling)

    # Restoring a piece for more than it will ever be worth is not a restoration estimate.
    if restored_value_cents and cost > restored_value_cents:
        notes.append(
            f"materials cut from ${cost / 100:.0f} to the restored value "
            f"(${restored_value_cents / 100:.0f}) — no one spends more than the piece fetches"
        )
        cost = max(MIN_MATERIALS_CENTS, restored_value_cents)

    return RestorationBounds(cost_cents=cost, effort_hours=hours, adjustments=notes)
