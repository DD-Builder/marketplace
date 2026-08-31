"""Acquisition cost: a parcel for small lots, a real drive for bulky ones.

The auction side buys finished pieces to resell as-found, so *getting the thing home*
is the cost line that decides marginal lots — not restoration. A $35 padded envelope
and a 166-mile round trip to Cincinnati differ by enough to flip a bid either way, so
the two must never collapse into one site-wide guess.
"""

from __future__ import annotations

from dealfinder.auctions.logistics import acquisition_cost
from dealfinder.verticals import DECOR, FURNITURE, JEWELRY, RUGS, WATCHES, get_vertical


def test_small_categories_ship_flat():
    for v in (JEWELRY, WATCHES, DECOR):
        got = acquisition_cost(v)
        assert not got.pickup
        assert got.cost_cents == 3500
        assert got.label == "Shipping"


def test_bulky_categories_are_charged_the_round_trip():
    for v in (FURNITURE, RUGS):
        got = acquisition_cost(v, hourly_rate_cents=3000)
        assert got.pickup
        assert got.label == "Pickup drive"
        # 83 mi each way at $0.70, plus 1.45h each way at $30/hr.
        assert got.cost_cents == round(166 * 70) + round(2.9 * 3000)


def test_the_drive_costs_several_times_what_a_parcel_does():
    """If these were close, splitting them wouldn't matter. They aren't."""
    assert acquisition_cost(FURNITURE).cost_cents > 4 * acquisition_cost(JEWELRY).cost_cents


def test_your_hourly_rate_moves_the_drive_but_not_the_parcel():
    cheap = acquisition_cost(FURNITURE, hourly_rate_cents=1000)
    dear = acquisition_cost(FURNITURE, hourly_rate_cents=9000)
    assert dear.cost_cents > cheap.cost_cents
    assert acquisition_cost(JEWELRY, hourly_rate_cents=1000).cost_cents == \
        acquisition_cost(JEWELRY, hourly_rate_cents=9000).cost_cents


def test_it_accepts_a_vertical_key_or_an_object_or_nothing():
    assert acquisition_cost("rugs").pickup
    assert not acquisition_cost("jewelry").pickup
    assert acquisition_cost(RUGS).pickup
    # Unknown/blank falls back to the default vertical (furniture), which is bulky —
    # the conservative side, since assuming free shipping would inflate every ceiling.
    assert acquisition_cost("").pickup
    assert acquisition_cost(None).pickup


def test_every_figure_is_overridable_from_the_environment(monkeypatch):
    monkeypatch.setenv("EBTH_SHIP_CENTS", "1200")
    assert acquisition_cost(JEWELRY).cost_cents == 1200

    monkeypatch.setenv("PICKUP_ONE_WAY_MILES", "10")
    monkeypatch.setenv("PICKUP_ONE_WAY_HOURS", "0.25")
    monkeypatch.setenv("MILEAGE_RATE_CENTS", "50")
    got = acquisition_cost(FURNITURE, hourly_rate_cents=2000)
    assert got.cost_cents == round(20 * 50) + round(0.5 * 2000)


def test_garbage_env_values_fall_back_instead_of_crashing_the_run(monkeypatch):
    monkeypatch.setenv("EBTH_SHIP_CENTS", "not-a-number")
    monkeypatch.setenv("PICKUP_ONE_WAY_MILES", "")
    assert acquisition_cost(JEWELRY).cost_cents == 3500
    assert acquisition_cost(FURNITURE).cost_cents > 0


def test_the_detail_line_explains_the_number_it_reports():
    """The board shows this verbatim — 'you have to go get this one' changes whether a
    thin margin is worth it, so the reason has to travel with the figure."""
    drive = acquisition_cost(FURNITURE)
    assert "round trip" in drive.detail and "your time" in drive.detail
    assert "shipping" in acquisition_cost(JEWELRY).detail.lower()


def test_new_verticals_are_registered_and_classified():
    for key in ("watches", "rugs", "decor"):
        assert get_vertical(key).key == key
    assert get_vertical("rugs").bulky
    assert not get_vertical("watches").bulky
