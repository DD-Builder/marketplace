"""Your books: logged costs feeding tier 2, and the realised history."""

from __future__ import annotations

from dealfinder.pieces import (
    Ledger,
    PieceLog,
    costs_by_id,
    history,
    load_ledger,
    outcomes,
    save_ledger,
    upsert,
)

RATE = 3000


def _p(pid, **kw):
    return PieceLog(listing_id=pid, **kw)


def test_only_pieces_with_something_entered_override_the_estimate():
    """An empty log must not replace 'bought at ask, restored per estimate' with a basis
    of zero — that would make every untouched piece look free."""
    led = Ledger(pieces={
        "empty": _p("empty"),
        "bought": _p("bought", acquired_price_cents=6000),
        "hours_only": _p("hours_only", labor_hours=3.0),
    })
    assert set(costs_by_id(led)) == {"bought", "hours_only"}


def test_history_reports_cash_profit_net_profit_and_your_real_wage():
    led = Ledger(pieces={
        "a": _p("a", title="credenza", acquired_price_cents=6000, materials_cents=4000,
                labor_hours=6.0, sold_price_cents=40000),
        "b": _p("b", title="side table", acquired_price_cents=3000, materials_cents=1000,
                labor_hours=2.0, sold_price_cents=9000),
        "unsold": _p("unsold", acquired_price_cents=5000, labor_hours=1.0),
    })
    h = history(led, RATE)

    assert h.sold_count == 2 and h.has_data
    assert h.cash_invested_cents == 14000
    assert h.revenue_cents == 49000
    assert h.cash_profit_cents == 35000                 # 49000 - 14000
    assert h.net_profit_cents == 35000 - 8 * RATE       # after valuing 8 hours
    assert h.hours == 8.0
    assert h.effective_hourly_cents == round(35000 / 8)
    assert h.best_piece == "credenza" and h.worst_piece == "side table"


def test_history_is_empty_until_something_actually_sells():
    led = Ledger(pieces={"a": _p("a", acquired_price_cents=6000, labor_hours=4.0)})
    h = history(led, RATE)
    assert not h.has_data and h.sold_count == 0 and h.effective_hourly_cents is None
    assert outcomes(led, RATE) == {}


def test_upsert_merges_rather_than_overwriting():
    led = Ledger()
    upsert(led, _p("a", title="dresser", acquired_price_cents=6000))
    upsert(led, _p("a", labor_hours=5.0))
    entry = led.pieces["a"]
    assert entry.title == "dresser" and entry.acquired_price_cents == 6000
    assert entry.labor_hours == 5.0
    assert entry.acquired_at is not None


def test_selling_stamps_the_sale_date():
    led = Ledger()
    upsert(led, _p("a", acquired_price_cents=6000))
    upsert(led, _p("a", sold_price_cents=20000))
    assert led.pieces["a"].is_sold and led.pieces["a"].sold_at is not None


def test_ledger_round_trips_and_degrades_safely(tmp_path):
    led = Ledger()
    upsert(led, _p("a", title="dresser", acquired_price_cents=6000, labor_hours=4.0))
    path = tmp_path / "pieces.json"
    save_ledger(led, path)
    assert load_ledger(path).pieces["a"].labor_hours == 4.0

    (tmp_path / "junk.json").write_text("{ not json")
    assert load_ledger(tmp_path / "junk.json").pieces == {}
    assert load_ledger(tmp_path / "missing.json").pieces == {}
