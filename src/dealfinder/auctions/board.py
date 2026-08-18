"""Render the auction board — a static page beside the main deal board.

Same contract as :mod:`dealfinder.board`: self-contained HTML, no server, regenerated
whole every run and committed. The page is organized around the one thing that matters
in an auction — the clock — and around one comparison: what a lot is worth versus what
it currently costs.

Everything is filterable. The summary tiles at the top are buttons, the category chips
filter by vertical, and the search box filters by text; all three compose. Clicking a
card opens the case for (or against) that lot: a plain-language writeup of what it is
and why the number is what it is, the full bid trajectory, and every comparable close
this tracker has actually observed.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dealfinder.auctions.bidding import BidGuidance
from dealfinder.auctions.catalog import AuctionCatalog, AuctionEntry, comparable_closes
from dealfinder.verticals import get_vertical

_STANCE = {
    "bid": ("BID LATE", "good"),
    "watch": ("WATCH", ""),
    "outpriced": ("OUTPRICED", "crit"),
    "no-value": ("PASS", "crit"),
}


def _money(cents: int | None, *, none: str = "—") -> str:
    if cents is None:
        return none
    sign = "−" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.0f}"


def _esc(text) -> str:
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
    decide_within_days: float = 2.0


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


def writeup(entry: AuctionEntry, g: BidGuidance | None) -> str:
    """One paragraph on what this is, why it's wanted, and why the number is the number.

    Assembled from the appraisal the model already returned rather than a second AI
    call: the identification, era, maker and condition read are exactly the substance of
    "why is this sought after", and the deal arithmetic is the rest. Composing them
    costs nothing per lot and can't hallucinate a fact the appraisal didn't contain.
    """
    a = entry.appraisal
    if a is None:
        return "Not appraised yet — this lot is tracked but hasn't been valued."

    bits: list[str] = []
    what = a.identified_item or entry.title or "This lot"
    era = f" ({a.style_era})" if a.style_era else ""
    maker = f" attributed to {a.maker_guess}" if a.maker_guess else ""
    bits.append(f"{what}{era}{maker}.")

    if a.reasoning:
        bits.append(a.reasoning.strip().rstrip(".") + ".")
    elif a.condition_assessment:
        bits.append(a.condition_assessment.strip().rstrip(".") + ".")

    if a.materials:
        bits.append("Materials read as " + ", ".join(a.materials[:4]) + ".")

    if g is not None:
        value, cur = g.value_cents, entry.current_bid_cents
        bits.append(
            f"Sold as it arrives it should fetch about {_money(value)}"
            f" (confidence {a.confidence:.0%})."
        )
        if cur is not None and g.margin_at_current_cents is not None:
            verb = "leaves" if g.margin_at_current_cents > 0 else "leaves a shortfall of"
            bits.append(
                f"At the current {_money(cur)} bid, winning costs "
                f"{_money(round(cur * 1.15) + g.logistics_cents)} all-in once the "
                f"buyer's premium and {g.logistics_label.lower()} "
                f"({_money(g.logistics_cents)}) are counted, which {verb} "
                f"{_money(abs(g.margin_at_current_cents))}."
            )
        bits.append(
            f"Bidding past {_money(g.max_bid_cents)} is where the margin stops being "
            "worth the trouble."
        )
    return " ".join(bits)


def _lot_payload(
    catalog: AuctionCatalog, entry: AuctionEntry, g: BidGuidance | None, *, now: datetime
) -> dict:
    """Everything the detail dialog needs for one lot, as plain JSON."""
    vert = get_vertical(entry.vertical) if entry.vertical else None
    return {
        "id": entry.id,
        "title": entry.title or entry.id,
        "url": entry.url,
        "photo": entry.photo_rel,
        "vertical": entry.vertical or "",
        "vertical_label": vert.label if vert else "",
        "stance": g.stance if g else "watch",
        "writeup": writeup(entry, g),
        "current_bid": entry.current_bid_cents,
        "bid_count": entry.bid_count,
        "ends_at": entry.ends_at.isoformat() if entry.ends_at else "",
        "value": g.value_cents if g else None,
        "max_bid": g.max_bid_cents if g else None,
        "all_in": g.all_in_at_max_cents if g else None,
        "projected": g.projected_final_cents if g else None,
        "margin_now": g.margin_at_current_cents if g else None,
        "logistics": g.logistics_detail if g else "",
        "notes": g.notes if g else [],
        # Real data only: this lot's own observed bid trajectory...
        "bids": [
            {"t": p.at.isoformat(), "v": p.bid_cents}
            for p in entry.bid_history if p.bid_cents is not None
        ],
        # ...and every comparable close this tracker actually watched happen.
        "comps": [
            {"t": when.isoformat(), "v": cents}
            for when, cents in comparable_closes(catalog, entry)
        ],
    }


def _card(entry: AuctionEntry, g: BidGuidance | None, *, now: datetime) -> str:
    label, tone = _STANCE.get(g.stance if g else "watch", ("WATCH", ""))
    left = entry.hours_left(now)
    ends_iso = entry.ends_at.isoformat() if entry.ends_at else ""
    photo = (
        f'<img src="{_esc(entry.photo_rel)}" alt="" loading="lazy">'
        if entry.photo_rel else '<div class="nophoto">no photo</div>'
    )
    a = entry.appraisal
    ident = _esc((a.identified_item if a else "")[:90]) or "not yet appraised"

    rows = [("Current bid", _money(entry.current_bid_cents)
             + (f" · {entry.bid_count} bids" if entry.bid_count else ""))]
    if g:
        rows.append(("Est. value", f"<b>{_money(g.value_cents)}</b>"))
        rows.append(("Your max bid", f"<b>{_money(g.max_bid_cents)}</b>"))
        if g.max_bid_cents > 0:
            rows.append(("All-in at max", _money(g.all_in_at_max_cents)))
            rows.append(("Projected close", _money(g.projected_final_cents)))
        if g.margin_at_current_cents is not None:
            tone_cls = "good" if g.margin_at_current_cents > 0 else "crit"
            rows.append(("Margin at bid",
                         f'<span class="{tone_cls}">'
                         f"{_money(g.margin_at_current_cents)}</span>"))
    body = "".join(
        f'<div class="row"><span>{k}</span><span class="v">{v}</span></div>'
        for k, v in rows
    )
    countdown = (
        f'<span class="clock" data-ends="{_esc(ends_iso)}">{_hours_label(left)}</span>'
        if entry.ends_at else '<span class="clock unknown">end time unknown</span>'
    )
    vert = entry.vertical or ""
    hay = _esc(f"{entry.title} {a.identified_item if a else ''} {vert}".lower())
    return f"""
    <article class="lot {tone}" data-stance="{_esc(g.stance if g else 'watch')}"
             data-vertical="{_esc(vert)}" data-hay="{hay}" data-id="{_esc(entry.id)}"
             tabindex="0" role="button" aria-label="Open details">
      <div class="media">{photo}<span class="stance {tone}">{label}</span></div>
      <div class="body">
        <h3>{_esc(entry.title or entry.id)}</h3>
        <p class="ident">{ident}</p>
        <div class="clockrow">{countdown}</div>
        {body}
        <p class="more">Details &amp; price history →</p>
      </div>
    </article>"""


def _ended_row(entry: AuctionEntry, g: BidGuidance | None) -> str:
    verdict = ""
    if g and entry.final_price_cents is not None and g.max_bid_cents:
        if entry.final_price_cents <= g.max_bid_cents:
            verdict = '<span class="tone good">closed under your max — a miss</span>'
        else:
            verdict = '<span class="tone">closed above your max — right to pass</span>'
    return (
        f"<tr><td>{_esc(entry.title or entry.id)}</td>"
        f"<td class='v'>{_money(entry.t24_bid_cents)}</td>"
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
    far = datetime.max.replace(tzinfo=timezone.utc)
    ending = sorted((e for e in watch if e.state == "ending"), key=lambda e: e.ends_at or far)
    live = sorted((e for e in watch if e.state != "ending"), key=lambda e: e.ends_at or far)
    ended = sorted(
        (e for e in catalog.lots.values() if e.state == "ended" and e.watch),
        key=lambda e: e.ends_at or e.last_seen, reverse=True,
    )[:25]

    actionable = sum(1 for g in guidance.values() if g.stance == "bid")
    verticals = sorted({e.vertical for e in watch if e.vertical})

    banner = ""
    if meta.state == "scan_blocked":
        banner = ('<p class="notice bad">EBTH could not be reached this run — countdowns '
                  "are live, but bids and new lots are as of the last successful scan.</p>")

    payload = {
        e.id: _lot_payload(catalog, e, guidance.get(e.id), now=now)
        for e in (*ending, *live)
    }

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

    chips = "".join(
        f'<button class="chip" data-vertical="{_esc(v)}">'
        f"{_esc(get_vertical(v).label)}</button>"
        for v in verticals
    )

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{_esc(meta.title)} — {_esc(meta.subtitle)}</title>
<style>{_CSS}</style>
</head><body>
<nav class="tabs">
  <a href="../index.html">Home</a>
  <a href="../board.html">The Bench · Marketplace</a>
  <a class="on" href="index.html">The Gavel · Auctions</a>
</nav>
<header class="top">
  <div class="brandrow">
    <h1>{_esc(meta.title)}</h1>
    <span class="tag">{_esc(meta.subtitle)}</span>
    <span class="sub">{_esc(meta.generated_at)}</span>
  </div>
  {banner}
  <div class="summary">
    <button class="stat" data-stance="all">
      <div class="n">{len(watch)}</div><div class="l">on watch</div></button>
    <button class="stat killer" data-stance="bid">
      <div class="n">{actionable}</div><div class="l">worth bidding</div></button>
    <button class="stat" data-stance="ending">
      <div class="n">{len(ending)}</div><div class="l">closing ≤24h</div></button>
    <button class="stat" data-stance="outpriced">
      <div class="n">{sum(1 for g in guidance.values() if g.stance == 'outpriced')}</div>
      <div class="l">outpriced</div></button>
    <button class="stat" data-stance="none" title="Endgame multiplier">
      <div class="n">{meta.multiplier:.1f}×</div>
      <div class="l">endgame ({meta.calibration_n} obs)</div></button>
  </div>
  <div class="controls">
    <div class="filters" id="chips">
      <button class="chip on" data-vertical="all">All categories</button>
      {chips}
    </div>
    <input id="q" type="search" placeholder="Search lots…" aria-label="Search lots">
    <button id="reset" class="chip">Reset</button>
  </div>
  <p class="sub note">Values are what a piece fetches <b>as it arrives</b> — nothing here
  assumes restoration. Max bids already subtract the {meta.premium_pct:.0%} buyer's premium
  and the cost of getting it home (flat shipping for small lots; the real Lexington↔
  Cincinnati round trip for furniture and rugs). Only lots closing within
  {meta.decide_within_days:.0f} days are valued. {_esc(meta.note)}</p>
</header>
<main>
  <section id="sec-ending"><h2>Closing soon
    <span class="hint">the only window where bidding pays</span></h2>
    <div class="grid">{ending_html}</div></section>
  <section id="sec-live"><h2>On watch
    <span class="hint">hold — early bids only feed the price</span></h2>
    <div class="grid">{live_html}</div></section>
  <p class="empty" id="noresults" hidden>Nothing matches those filters.</p>
  <section><h2>Recently ended
    <span class="hint">how the endgame actually behaves here</span></h2>
    {ended_html}</section>
</main>
<dialog id="detail"><div class="dlg"></div></dialog>
<footer><a href="../index.html">← Home</a></footer>
<script>window.LOTS = {json.dumps(payload)};</script>
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
  padding:0 env(safe-area-inset-right) 0 env(safe-area-inset-left)}
nav.tabs{display:flex;gap:2px;background:var(--card);border-bottom:1px solid var(--line);
  padding:0 20px;overflow-x:auto;position:sticky;top:0;z-index:5}
nav.tabs a{padding:14px 18px;color:var(--soft);text-decoration:none;font-size:14px;
  font-weight:600;white-space:nowrap;border-bottom:2px solid transparent}
nav.tabs a.on{color:var(--accent);border-bottom-color:var(--accent)}
nav.tabs a:hover{color:var(--ink)}
header.top,main,footer{max-width:1200px;margin:0 auto;padding:0 20px}
header.top{padding-top:22px}
.brandrow{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-family:var(--display);font-size:34px;margin:0;font-weight:600}
.tag{font-size:12px;text-transform:uppercase;letter-spacing:.14em;color:var(--teal);
  border:1.5px solid var(--teal);border-radius:999px;padding:4px 12px}
.sub{color:var(--soft);font-size:13px}
.note{max-width:70ch;line-height:1.6}
.notice{font-size:14px;border-radius:10px;padding:10px 14px;margin:12px 0 0}
.notice.bad{background:var(--crit-bg);color:var(--crit);border:1px solid var(--crit)}
.summary{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:10px 16px;box-shadow:var(--shadow);min-width:96px;cursor:pointer;
  font:inherit;color:inherit;text-align:left}
.stat:hover{border-color:var(--accent)}
.stat[aria-pressed="true"]{border-color:var(--accent);background:var(--warn-bg)}
.stat .n{font-family:var(--display);font-size:24px;font-weight:600}
.stat .l{font-size:11px;color:var(--soft);text-transform:uppercase;letter-spacing:.09em}
.stat.killer .n{color:var(--accent)}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:14px 0 0}
.filters{display:flex;gap:8px;flex-wrap:wrap}
.chip{font:inherit;font-size:13.5px;min-height:40px;padding:8px 14px;border-radius:999px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.chip.on{background:var(--teal);border-color:var(--teal);color:#fff;font-weight:600}
@media (prefers-color-scheme:dark){.chip.on{color:#10201c}}
#q{font:inherit;font-size:14px;min-height:40px;padding:8px 14px;border-radius:999px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);min-width:200px}
h2{font-family:var(--display);font-size:22px;margin:34px 0 12px}
h2 .hint{font-family:var(--sans);font-size:12.5px;color:var(--soft);font-weight:400;
  margin-left:8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:16px}
.lot{background:var(--card);border:1px solid var(--line);border-radius:14px;
  overflow:hidden;box-shadow:var(--shadow);display:flex;flex-direction:column;
  cursor:pointer;transition:transform .12s ease,border-color .12s ease}
.lot:hover,.lot:focus{transform:translateY(-2px);border-color:var(--accent);outline:none}
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
.good{color:var(--good)} .crit{color:var(--crit)}
.more{font-size:12.5px;color:var(--teal);margin:8px 0 0;font-weight:600}
.empty{color:var(--soft);font-size:14px}
table{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--card);
  border:1px solid var(--line);border-radius:14px;overflow:hidden}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line)}
th{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--soft)}
td.v{font-family:var(--mono);font-variant-numeric:tabular-nums}
.tone{font-size:12px;color:var(--soft)} .tone.good{color:var(--good);font-weight:650}
footer{padding:40px 20px;color:var(--soft);font-size:14px}
footer a{color:var(--teal);text-decoration:none;font-weight:600}
dialog{border:none;border-radius:16px;padding:0;max-width:720px;width:calc(100% - 32px);
  background:var(--card);color:var(--ink);box-shadow:0 10px 60px #0006}
dialog::backdrop{background:#0009}
.dlg{padding:22px 24px 26px}
.dlg h3{font-family:var(--display);font-size:24px;margin:0 0 4px}
.dlg .writeup{font-size:14.5px;line-height:1.65;margin:14px 0}
.dlg .figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;
  margin:16px 0}
.fig{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.fig .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--soft)}
.fig .val{font-family:var(--mono);font-size:16px;font-weight:650}
.chart{margin:18px 0 6px}
.chart h4{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--soft);
  margin:0 0 6px}
.chart svg{width:100%;height:150px;display:block;background:var(--paper);
  border:1px solid var(--line);border-radius:10px}
.thin{font-size:12.5px;color:var(--soft);margin:6px 0 0;line-height:1.55}
.dlg .close{position:absolute;top:14px;right:18px;background:none;border:none;
  font-size:26px;color:var(--soft);cursor:pointer;line-height:1}
.dlg .out{display:inline-block;margin-top:10px;color:var(--teal);text-decoration:none;
  font-weight:600;font-size:14px}
"""

_JS = r"""
// ---- countdowns ----------------------------------------------------------------
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

// ---- filtering: stance + category + text, all composable -----------------------
var state = {stance: 'all', vertical: 'all', q: ''};

function apply(){
  var shown = 0;
  document.querySelectorAll('.lot').forEach(function(card){
    var okStance = state.stance === 'all' ||
      (state.stance === 'ending' ? card.closest('#sec-ending') !== null
                                 : card.dataset.stance === state.stance);
    var okVert = state.vertical === 'all' || card.dataset.vertical === state.vertical;
    var okText = !state.q || (card.dataset.hay || '').indexOf(state.q) !== -1;
    var show = okStance && okVert && okText;
    card.hidden = !show;
    if (show) shown++;
  });
  // Hide a section whose cards are all filtered out, so we don't leave bare headings.
  ['sec-ending','sec-live'].forEach(function(id){
    var sec = document.getElementById(id);
    if (!sec) return;
    var any = sec.querySelector('.lot:not([hidden])');
    sec.hidden = !any && (state.stance !== 'all' || state.vertical !== 'all' || !!state.q);
  });
  var none = document.getElementById('noresults');
  if (none) none.hidden = shown !== 0;
  document.querySelectorAll('.stat[data-stance]').forEach(function(b){
    b.setAttribute('aria-pressed', String(b.dataset.stance === state.stance));
  });
  document.querySelectorAll('#chips .chip').forEach(function(c){
    c.classList.toggle('on', c.dataset.vertical === state.vertical);
  });
}

document.querySelectorAll('.stat[data-stance]').forEach(function(btn){
  btn.addEventListener('click', function(){
    var v = btn.dataset.stance;
    if (v === 'none') return;                  // the multiplier tile isn't a filter
    state.stance = (state.stance === v) ? 'all' : v;   // click again to clear
    apply();
  });
});
document.querySelectorAll('#chips .chip').forEach(function(chip){
  chip.addEventListener('click', function(){
    state.vertical = chip.dataset.vertical;
    apply();
  });
});
var q = document.getElementById('q');
if (q) q.addEventListener('input', function(){
  state.q = q.value.trim().toLowerCase(); apply();
});
var reset = document.getElementById('reset');
if (reset) reset.addEventListener('click', function(){
  state = {stance:'all', vertical:'all', q:''};
  if (q) q.value = '';
  apply();
});

// ---- charts: only ever real observed data --------------------------------------
function money(c){
  if (c === null || c === undefined) return '—';
  return '$' + Math.round(c/100).toLocaleString();
}
function svgLine(points, opts){
  opts = opts || {};
  if (!points.length) return '';
  var W = 600, H = 150, P = 26;
  var xs = points.map(function(p){ return p.x; });
  var ys = points.map(function(p){ return p.y; });
  var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
  var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
  if (x1 === x0) x1 = x0 + 1;
  if (y1 === y0) { y1 = y0 + Math.max(1, y0 * 0.1); y0 = Math.max(0, y0 - y0 * 0.1); }
  var sx = function(x){ return P + (x - x0) / (x1 - x0) * (W - P * 2); };
  var sy = function(y){ return H - P - (y - y0) / (y1 - y0) * (H - P * 2); };
  var d = points.map(function(p, i){
    return (i ? 'L' : 'M') + sx(p.x).toFixed(1) + ' ' + sy(p.y).toFixed(1);
  }).join(' ');
  var dots = points.map(function(p){
    return '<circle cx="' + sx(p.x).toFixed(1) + '" cy="' + sy(p.y).toFixed(1) +
           '" r="3" fill="var(--accent)"><title>' + money(p.y) + '</title></circle>';
  }).join('');
  var path = opts.scatter ? '' :
    '<path d="' + d + '" fill="none" stroke="var(--accent)" stroke-width="2"/>';
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
    '<text x="4" y="14" font-size="11" fill="var(--soft)">' + money(y1) + '</text>' +
    '<text x="4" y="' + (H - 6) + '" font-size="11" fill="var(--soft)">' +
      money(y0) + '</text>' + path + dots + '</svg>';
}

// ---- detail dialog -------------------------------------------------------------
var dlg = document.getElementById('detail');
function openLot(id){
  var d = (window.LOTS || {})[id];
  if (!d || !dlg) return;
  var figs = [
    ['Est. value (as-is)', money(d.value)],
    ['Current bid', money(d.current_bid)],
    ['Your max bid', money(d.max_bid)],
    ['All-in at max', money(d.all_in)],
    ['Projected close', money(d.projected)],
    ['Margin at bid', money(d.margin_now)]
  ].map(function(f){
    return '<div class="fig"><div class="k">' + f[0] + '</div><div class="val">' +
           f[1] + '</div></div>';
  }).join('');

  var bids = (d.bids || []).map(function(p){
    return {x: Date.parse(p.t), y: p.v};
  }).filter(function(p){ return !isNaN(p.x); });
  var comps = (d.comps || []).map(function(p){
    return {x: Date.parse(p.t), y: p.v};
  }).filter(function(p){ return !isNaN(p.x); });

  var bidChart = bids.length > 1
    ? '<div class="chart"><h4>Bidding on this lot</h4>' + svgLine(bids) + '</div>'
    : '';
  var span = comps.length > 1
    ? Math.round((comps[comps.length-1].x - comps[0].x) / 864e5) : 0;
  var compChart = comps.length
    ? '<div class="chart"><h4>Comparable closes we have observed (' + comps.length +
      ' lots' + (span ? ', ' + span + ' days' : '') + ')</h4>' +
      svgLine(comps, {scatter: comps.length < 4}) +
      (comps.length < 8
        ? '<p class="thin">Too few closes so far to read a trend — this fills in as ' +
          'the tracker watches more lots close.</p>'
        : '') + '</div>'
    : '<div class="chart"><h4>Comparable closes</h4><p class="thin">None observed yet. ' +
      'There is no public feed of long-run realised auction prices, so rather than ' +
      'draw a trend line from data we do not have, this chart plots only lots this ' +
      'tracker has actually watched close. It fills in from here.</p></div>';

  var notes = (d.notes || []).map(function(n){
    return '<p class="thin">' + n + '</p>';
  }).join('');

  dlg.querySelector('.dlg').innerHTML =
    '<button class="close" aria-label="Close">×</button>' +
    '<h3>' + d.title + '</h3>' +
    '<p class="sub">' + (d.vertical_label || '') + '</p>' +
    '<p class="writeup">' + d.writeup + '</p>' +
    '<div class="figs">' + figs + '</div>' +
    (d.logistics ? '<p class="thin">' + d.logistics + '</p>' : '') +
    notes + bidChart + compChart +
    (d.url ? '<a class="out" href="' + d.url + '" target="_blank" rel="noopener">' +
             'View on EBTH ↗</a>' : '');
  dlg.querySelector('.close').addEventListener('click', function(){ dlg.close(); });
  dlg.showModal();
}
document.addEventListener('click', function(e){
  var card = e.target.closest('.lot');
  if (card) openLot(card.dataset.id);
});
document.addEventListener('keydown', function(e){
  if (e.key === 'Enter' && document.activeElement &&
      document.activeElement.classList.contains('lot')) {
    openLot(document.activeElement.dataset.id);
  }
});
if (dlg) dlg.addEventListener('click', function(e){ if (e.target === dlg) dlg.close(); });
apply();
"""
