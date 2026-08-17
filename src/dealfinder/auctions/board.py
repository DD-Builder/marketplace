"""Render the auction board — a static page beside the main deal board.

Same contract as :mod:`dealfinder.board`: self-contained HTML, no server, regenerated
whole every run and committed. The page is organized around the one thing that matters
in an auction — the clock. Lots inside the final day lead with a live countdown and a
stance; everything else is a quiet watchlist the hourly job keeps warm.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dealfinder.auctions.bidding import BidGuidance
from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry

_STANCE = {
    "bid": ("BID LATE", "good"),
    "watch": ("WATCH", ""),
    "outpriced": ("OUTPRICED", "crit"),
    "no-value": ("PASS", "crit"),
}


def _money(cents: int | None, *, none: str = "—") -> str:
    if cents is None:
        return none
    return f"${cents / 100:,.0f}"


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


@dataclass
class AuctionBoardMeta:
    title: str = "The Gavel"
    subtitle: str = "EBTH auction watch"
    generated_at: str = ""
    note: str = ""
    premium_pct: float = 0.15
    multiplier: float = 2.0
    calibration_n: int = 0
    state: str = "ok"          # ok | scan_blocked — drives the banner


def _card(entry: AuctionEntry, g: BidGuidance | None, *, now: datetime) -> str:
    label, tone = _STANCE.get(g.stance if g else "watch", ("WATCH", ""))
    left = entry.hours_left(now)
    ends_iso = entry.ends_at.isoformat() if entry.ends_at else ""
    photo = (
        f'<img src="{_esc(entry.photo_rel)}" alt="" loading="lazy">'
        if entry.photo_rel else '<div class="nophoto">no photo</div>'
    )
    appr = entry.appraisal
    ident = _esc(appr.identified_item[:90]) if appr else "not yet appraised"

    rows = [
        ("Current bid", _money(entry.current_bid_cents)
         + (f" · {entry.bid_count} bids" if entry.bid_count else "")),
    ]
    if g:
        rows.append(("Your max bid", f"<b>{_money(g.max_bid_cents)}</b>"))
        if g.max_bid_cents > 0:
            # "What winning at your ceiling costs" is meaningless when the ceiling is
            # zero — a PASS card showing "all-in $15" against a $0 max bid reads broken.
            rows += [
                ("All-in at max", _money(g.all_in_at_max_cents)),
                ("Projected close", _money(g.projected_final_cents)),
            ]
        if g.velocity_cents_per_hour:
            rows.append(("Moving", f"{_money(round(g.velocity_cents_per_hour))}/hr"))
        if appr:
            rows.append(("Restored value", _money(appr.est_restored_resale_value_cents)
                         + f" · conf {appr.confidence:.0%}"))
    body = "".join(
        f'<div class="row"><span>{k}</span><span class="v">{v}</span></div>'
        for k, v in rows
    )
    reason = f'<p class="reason">{_esc(g.reason)}</p>' if g else ""
    notes = "".join(f'<p class="note">{_esc(n)}</p>' for n in (g.notes if g else []))
    countdown = (
        f'<span class="clock" data-ends="{_esc(ends_iso)}">'
        f"{_hours_label(left)}</span>" if entry.ends_at else
        '<span class="clock unknown">end time unknown</span>'
    )
    link = (f'<a class="out" href="{_esc(entry.url)}" target="_blank" rel="noopener">'
            "View on EBTH ↗</a>" if entry.url else "")
    return f"""
    <article class="lot {tone}">
      <div class="media">{photo}<span class="stance {tone}">{label}</span></div>
      <div class="body">
        <h3>{_esc(entry.title or entry.id)}</h3>
        <p class="ident">{ident}</p>
        <div class="clockrow">{countdown}</div>
        {body}
        {reason}{notes}
        {link}
      </div>
    </article>"""


def _hours_label(left: float | None) -> str:
    if left is None:
        return "—"
    if left <= 0:
        return "ended"
    if left < 1:
        return f"{int(left * 60)} min left"
    if left < 48:
        return f"{left:.0f}h left"
    return f"{left / 24:.0f} days left"


def _ended_row(entry: AuctionEntry, g: BidGuidance | None) -> str:
    verdict = ""
    if g and entry.final_price_cents is not None:
        if g.max_bid_cents and entry.final_price_cents <= g.max_bid_cents:
            verdict = '<span class="tone good">closed under your max — a miss</span>'
        else:
            verdict = '<span class="tone">closed above your max — right to pass</span>'
    t24 = _money(entry.t24_bid_cents)
    return (
        f"<tr><td>{_esc(entry.title or entry.id)}</td>"
        f"<td class='v'>{t24}</td>"
        f"<td class='v'>{_money(entry.final_price_cents)}</td>"
        f"<td class='v'>{_money(g.max_bid_cents) if g else '—'}</td>"
        f"<td>{verdict}</td></tr>"
    )


def write_auction_page(
    catalog: AuctionCatalog,
    guidance: dict[str, BidGuidance],
    out_dir: Path,
    *,
    meta: AuctionBoardMeta,
    now: datetime | None = None,
) -> Path:
    now = now or datetime.now(timezone.utc)
    out_dir.mkdir(parents=True, exist_ok=True)

    watch = [e for e in catalog.lots.values() if e.watch and e.state in ("live", "ending")]
    ending = sorted(
        (e for e in watch if e.state == "ending"),
        key=lambda e: e.ends_at or datetime.max.replace(tzinfo=timezone.utc),
    )
    live = sorted(
        (e for e in watch if e.state == "live"),
        key=lambda e: e.ends_at or datetime.max.replace(tzinfo=timezone.utc),
    )
    ended = sorted(
        (e for e in catalog.lots.values() if e.state == "ended" and e.watch),
        key=lambda e: e.ends_at or e.last_seen, reverse=True,
    )[:20]

    actionable = sum(1 for g in guidance.values() if g.stance == "bid")

    banner = ""
    if meta.state == "scan_blocked":
        banner = ('<p class="notice bad">EBTH could not be reached this run — countdowns '
                  "are live, but bids and new lots are as of the last successful scan.</p>")

    ending_html = "".join(_card(e, guidance.get(e.id), now=now) for e in ending) \
        or '<p class="empty">Nothing closing within 24 hours.</p>'
    live_html = "".join(_card(e, guidance.get(e.id), now=now) for e in live) \
        or '<p class="empty">Nothing on watch yet — discovery fills this in.</p>'
    ended_html = (
        "<table><thead><tr><th>Lot</th><th>T-24h</th><th>Final</th><th>Your max</th>"
        "<th></th></tr></thead><tbody>"
        + "".join(_ended_row(e, guidance.get(e.id)) for e in ended)
        + "</tbody></table>"
    ) if ended else '<p class="empty">No closed lots observed yet.</p>'

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{_esc(meta.title)} — {_esc(meta.subtitle)}</title>
<style>{_CSS}</style>
</head><body>
<header class="top">
  <div class="brandrow">
    <h1>{_esc(meta.title)}</h1>
    <span class="tag">{_esc(meta.subtitle)}</span>
    <span class="sub">{_esc(meta.generated_at)}</span>
  </div>
  {banner}
  <div class="summary">
    <div class="stat"><div class="n">{len(ending)}</div><div class="l">closing ≤24h</div></div>
    <div class="stat killer"><div class="n">{actionable}</div><div class="l">worth bidding</div></div>
    <div class="stat"><div class="n">{len(live)}</div><div class="l">on watch</div></div>
    <div class="stat"><div class="n">{meta.multiplier:.1f}×</div>
      <div class="l">endgame ({meta.calibration_n} obs)</div></div>
  </div>
  <p class="sub note">Max bids assume a {meta.premium_pct:.0%} buyer's premium on the
  hammer; taxes and freight vary by lot — confirm both before bidding. {_esc(meta.note)}</p>
</header>
<main>
  <section><h2>Closing soon <span class="hint">the only window where bidding pays</span></h2>
    <div class="grid">{ending_html}</div></section>
  <section><h2>On watch <span class="hint">hold — early bids only feed the price</span></h2>
    <div class="grid">{live_html}</div></section>
  <section><h2>Recently ended <span class="hint">how the endgame actually behaves here</span></h2>
    {ended_html}</section>
</main>
<footer><a href="../index.html">← The Bench (Marketplace board)</a></footer>
<script>{_JS}</script>
</body></html>"""

    out = out_dir / "index.html"
    out.write_text(page, encoding="utf-8")
    return out


# Palette lifted from templates/board.css so both boards read as one product.
_CSS = """
:root{
  --paper:#f4efe4; --card:#fbf7ec; --ink:#2b2118; --soft:#6b5c49; --line:#e0d5c2;
  --accent:#a8431a; --teal:#2f6d62; --good:#2f6d62; --warn:#8a5a10; --warn-bg:#f3e6c8;
  --crit:#9a3524; --crit-bg:#f4ded6;
  --shadow:0 1px 0 #fff9, 0 2px 12px #6b5c4922;
  --display:"Iowan Old Style","Palatino Nova",Palatino,"Book Antiqua",Georgia,serif;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono","Roboto Mono",Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#211a13; --card:#2c241b; --ink:#f2e8d8; --soft:#b3a48d; --line:#4a3e2f;
  --accent:#e8804c; --teal:#6fbfae; --good:#6fbfae; --warn:#e0b45e; --warn-bg:#3d3016;
  --crit:#e08a72; --crit-bg:#42241b;
  --shadow:0 1px 0 #ffffff0a, 0 3px 16px #00000059;}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  line-height:1.45;-webkit-font-smoothing:antialiased;
  padding:env(safe-area-inset-top) env(safe-area-inset-right) 0 env(safe-area-inset-left)}
header.top,main,footer{max-width:1200px;margin:0 auto;padding:0 20px}
header.top{padding-top:28px}
.brandrow{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-family:var(--display);font-size:34px;margin:0;font-weight:600}
.tag{font-size:12px;text-transform:uppercase;letter-spacing:.14em;color:var(--teal);
  border:1.5px solid var(--teal);border-radius:999px;padding:4px 12px}
.sub{color:var(--soft);font-size:13px}
.notice{font-size:14px;border-radius:10px;padding:10px 14px;margin:12px 0 0}
.notice.bad{background:var(--crit-bg);color:var(--crit);border:1px solid var(--crit)}
.summary{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:10px 16px;box-shadow:var(--shadow);min-width:96px}
.stat .n{font-family:var(--display);font-size:24px;font-weight:600}
.stat .l{font-size:11px;color:var(--soft);text-transform:uppercase;letter-spacing:.09em}
.stat.killer .n{color:var(--accent)}
h2{font-family:var(--display);font-size:22px;margin:34px 0 12px}
h2 .hint{font-family:var(--sans);font-size:12.5px;color:var(--soft);font-weight:400;margin-left:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:16px}
.lot{background:var(--card);border:1px solid var(--line);border-radius:14px;
  overflow:hidden;box-shadow:var(--shadow);display:flex;flex-direction:column}
.lot.crit{border-color:var(--crit)}
.media{position:relative;aspect-ratio:4/3;background:var(--line)}
.media img{width:100%;height:100%;object-fit:cover;display:block}
.nophoto{display:flex;align-items:center;justify-content:center;height:100%;
  color:var(--soft);font-size:13px}
.stance{position:absolute;top:10px;left:10px;font-size:11px;font-weight:700;
  letter-spacing:.1em;padding:4px 10px;border-radius:999px;background:var(--warn-bg);
  color:var(--warn)}
.stance.good{background:var(--teal);color:#fff}
.stance.crit{background:var(--crit-bg);color:var(--crit)}
.body{padding:14px 16px 16px;display:flex;flex-direction:column;gap:6px}
h3{font-size:15.5px;margin:0;font-weight:650}
.ident{margin:0;font-size:13px;color:var(--soft)}
.clockrow{margin:2px 0 4px}
.clock{font-family:var(--mono);font-size:14px;font-weight:700;color:var(--accent)}
.clock.unknown{color:var(--soft);font-weight:400}
.row{display:flex;justify-content:space-between;font-size:13.5px;gap:12px}
.row span:first-child{color:var(--soft)}
.v,.clock{font-variant-numeric:tabular-nums}
.v{font-family:var(--mono)}
.reason{font-size:13px;margin:6px 0 0;line-height:1.5}
.note{font-size:12px;color:var(--soft);margin:2px 0 0}
.out{font-size:13px;color:var(--teal);margin-top:8px;text-decoration:none;font-weight:600}
.empty{color:var(--soft);font-size:14px}
table{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--card);
  border:1px solid var(--line);border-radius:14px;overflow:hidden}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line)}
th{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--soft)}
td.v{font-family:var(--mono);font-variant-numeric:tabular-nums}
.tone{font-size:12px;color:var(--soft)} .tone.good{color:var(--good);font-weight:650}
footer{padding:40px 20px;color:var(--soft);font-size:14px}
footer a{color:var(--teal);text-decoration:none;font-weight:600}
"""

# Countdown ticks client-side so the page stays honest between hourly rebuilds.
_JS = """
function tick(){
  document.querySelectorAll('.clock[data-ends]').forEach(function(el){
    var end = Date.parse(el.dataset.ends);
    if (isNaN(end)) return;
    var ms = end - Date.now();
    if (ms <= 0) { el.textContent = 'ended'; return; }
    var h = ms / 36e5;
    el.textContent = h < 1 ? Math.floor(ms/6e4) + ' min left'
                   : h < 48 ? Math.round(h) + 'h left'
                   : Math.round(h/24) + ' days left';
    if (h < 2) el.style.color = 'var(--crit)';
  });
}
tick(); setInterval(tick, 30000);
"""
