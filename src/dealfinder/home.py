"""The landing page: the best of every source in one ranked wall.

Two boards now feed this project — Marketplace pieces you buy and restore, EBTH lots you
buy finished and resell as-found — and they answer different questions with different
arithmetic. What they have in common is the only thing this page shows: *here is
something worth your attention, here is what it's worth, here is what it costs, and here
is how long you have to decide.*

Deliberately regenerated from the committed catalogues rather than from a run's
in-memory results, so whichever job finishes last refreshes the page and it always
agrees with what's actually on disk. That also means it costs nothing — no scrape, no
AI call — and can be rebuilt any time.
"""

from __future__ import annotations

import argparse
import html
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dealfinder.auctions import bidding
from dealfinder.auctions.catalog import (
    AuctionCatalog,
    load_auction_catalog,
)
from dealfinder.catalog import Catalog, load_catalog
from dealfinder.logging import get_logger

log = get_logger(__name__)


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _money(cents: int | None, *, none: str = "—") -> str:
    if cents is None:
        return none
    sign = "−" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.0f}"


@dataclass
class Pick:
    """One surfaced item, normalised across sources so the page can rank them together."""

    source: str            # "marketplace" | "auction"
    source_label: str
    id: str
    title: str
    url: str
    photo: str | None
    value_cents: int       # what it's worth on resale
    cost_cents: int        # what it costs right now (ask, or current bid)
    margin_cents: int      # value - cost, net of the costs each side actually bears
    detail: str            # the one-line reason it's here
    href: str              # which board to open for the full case
    ends_at: str = ""      # ISO, auctions only — drives the countdown
    badges: list[str] = field(default_factory=list)

    @property
    def multiple(self) -> float:
        """Value as a multiple of cost — the cross-source ranking key. A $40 lot worth
        $400 and a $400 piece worth $4,000 are equally good finds in the only sense that
        matters when deciding what to look at first."""
        return (self.value_cents / self.cost_cents) if self.cost_cents > 0 else 0.0


def marketplace_picks(catalog: Catalog, *, limit: int = 12) -> list[Pick]:
    """Best live Marketplace pieces, by restored value against what you'd pay.

    This side *does* assume restoration — that's the Bench's whole model — so the margin
    subtracts the estimated restoration cost.
    """
    picks: list[Pick] = []
    for e in catalog.listings.values():
        if e.state != "live" or e.appraisal is None:
            continue
        a = e.appraisal
        ask = e.asking_price_cents or 0
        if ask <= 0:
            continue
        value = a.est_restored_resale_value_cents
        margin = value - ask - a.est_restoration_cost_cents
        if margin <= 0:
            continue
        badges = []
        if a.confidence >= 0.7:
            badges.append("confident")
        if e.was_price_cents and ask < e.was_price_cents:
            badges.append("price drop")
        picks.append(Pick(
            source="marketplace", source_label="Marketplace",
            id=e.id, title=e.title or a.identified_item or e.id,
            url=e.url, photo=e.photo_rel,
            value_cents=value, cost_cents=ask, margin_cents=margin,
            detail=(
                f"{a.identified_item or 'Piece'} — asking {_money(ask)}, worth about "
                f"{_money(value)} restored ({_money(a.est_restoration_cost_cents)} of work)."
            ),
            href="board.html", badges=badges,
        ))
    picks.sort(key=lambda p: p.multiple, reverse=True)
    return picks[:limit]


def auction_picks(
    catalog: AuctionCatalog, *, limit: int = 12, now: datetime | None = None
) -> list[Pick]:
    """Best live EBTH lots — only ones the tracker says are actually worth bidding on.

    Nothing here assumes restoration: the value is the as-is resale estimate and the
    margin is already net of the buyer's premium and getting the thing home.
    """
    now = now or datetime.now(timezone.utc)
    pairs = [
        (e.t24_bid_cents, e.final_price_cents)
        for e in catalog.lots.values()
        if e.state == "ended" and e.final_price_cents and e.t24_bid_cents
    ]
    multiplier = bidding.endgame_multiplier(pairs)

    picks: list[Pick] = []
    for e in catalog.lots.values():
        if not e.watch or e.state not in ("live", "ending") or e.appraisal is None:
            continue
        g = bidding.guide(e, multiplier=multiplier, calibration_n=len(pairs), now=now)
        if g is None or g.stance != "bid" or g.margin_at_current_cents is None:
            continue
        if g.margin_at_current_cents <= 0:
            continue
        left = e.hours_left(now)
        badges = ["closing today"] if (left is not None and left <= 24) else []
        picks.append(Pick(
            source="auction", source_label="EBTH auction",
            id=e.id, title=e.title or e.id,
            url=e.url, photo=(f"auctions/{e.photo_rel}" if e.photo_rel else None),
            value_cents=g.value_cents,
            cost_cents=e.current_bid_cents or 0,
            margin_cents=g.margin_at_current_cents,
            detail=(
                f"Bid {_money(e.current_bid_cents)} against about "
                f"{_money(g.value_cents)} as-is. Max bid {_money(g.max_bid_cents)}."
            ),
            href="auctions/index.html",
            ends_at=e.ends_at.isoformat() if e.ends_at else "",
            badges=badges,
        ))
    picks.sort(key=lambda p: (bool(p.ends_at), p.multiple), reverse=True)
    return picks[:limit]


def _card(p: Pick) -> str:
    photo = (
        f'<img src="{_esc(p.photo)}" alt="" loading="lazy">'
        if p.photo else '<div class="nophoto">no photo</div>'
    )
    clock = (
        f'<span class="clock" data-ends="{_esc(p.ends_at)}"></span>'
        if p.ends_at else ""
    )
    badges = "".join(f'<span class="badge">{_esc(b)}</span>' for b in p.badges)
    hay = _esc(f"{p.title} {p.detail} {p.source_label}".lower())
    return f"""
    <a class="pick" href="{_esc(p.href)}" data-source="{_esc(p.source)}" data-hay="{hay}">
      <div class="media">{photo}<span class="src {_esc(p.source)}">
        {_esc(p.source_label)}</span></div>
      <div class="body">
        <h3>{_esc(p.title[:80])}</h3>
        <div class="clockrow">{clock}{badges}</div>
        <div class="row"><span>Costs now</span><span class="v">
          {_money(p.cost_cents)}</span></div>
        <div class="row"><span>Worth</span><span class="v">
          <b>{_money(p.value_cents)}</b></span></div>
        <div class="row"><span>Margin</span><span class="v good">
          {_money(p.margin_cents)}</span></div>
        <p class="why">{_esc(p.detail)}</p>
      </div>
    </a>"""


def render_home(
    picks: list[Pick],
    *,
    generated_at: str,
    marketplace_total: int,
    auction_total: int,
    notes: list[str] | None = None,
) -> str:
    cards = "".join(_card(p) for p in picks) or (
        '<p class="empty">Nothing surfaced yet — the boards fill this in as they run.</p>'
    )
    note_html = "".join(f'<p class="notice">{_esc(n)}</p>' for n in (notes or []))
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>The Find — best of every source</title>
<style>{_CSS}</style>
</head><body>
<nav class="tabs">
  <a class="on" href="index.html">Home</a>
  <a href="board.html">The Bench · Marketplace</a>
  <a href="auctions/index.html">The Gavel · Auctions</a>
</nav>
<header class="top">
  <div class="brandrow">
    <h1>The Find</h1>
    <span class="tag">best of every source</span>
    <span class="sub">{_esc(generated_at)}</span>
  </div>
  {note_html}
  <div class="summary">
    <button class="stat" data-source="all">
      <div class="n">{len(picks)}</div><div class="l">surfaced</div></button>
    <button class="stat" data-source="marketplace">
      <div class="n">{sum(1 for p in picks if p.source == 'marketplace')}</div>
      <div class="l">marketplace</div></button>
    <button class="stat killer" data-source="auction">
      <div class="n">{sum(1 for p in picks if p.source == 'auction')}</div>
      <div class="l">auctions</div></button>
  </div>
  <div class="controls">
    <input id="q" type="search" placeholder="Search everything…" aria-label="Search">
    <button id="reset" class="chip">Reset</button>
  </div>
  <p class="sub note">Ranked by what a thing is worth against what it costs right now.
  Marketplace pieces are valued restored (that's the work you'd put in); auction lots are
  valued <b>as they arrive</b>, already net of the buyer's premium and getting them home.
  Tracking {marketplace_total} live listings and {auction_total} live lots.</p>
</header>
<main>
  <div class="grid">{cards}</div>
  <p class="empty" id="noresults" hidden>Nothing matches that search.</p>
</main>
<footer>
  <a href="board.html">The Bench →</a> &nbsp;·&nbsp;
  <a href="auctions/index.html">The Gavel →</a>
</footer>
<script>{_JS}</script>
</body></html>"""


def build(
    docs: Path,
    *,
    now: datetime | None = None,
    limit: int = 24,
) -> Path:
    """Read both committed catalogues and write ``docs/index.html``."""
    now = now or datetime.now(timezone.utc)
    docs = Path(docs)
    notes: list[str] = []

    market = Catalog()
    try:
        if (docs / "catalog.json").exists():
            market = load_catalog(docs / "catalog.json")
    except Exception as exc:  # noqa: BLE001 — one bad catalogue must not blank the page
        notes.append("The Marketplace catalogue could not be read this run.")
        log.warning("home_marketplace_unreadable", error=str(exc)[:160])

    auctions = AuctionCatalog()
    try:
        if (docs / "auctions" / "catalog.json").exists():
            auctions = load_auction_catalog(docs / "auctions" / "catalog.json")
    except Exception as exc:  # noqa: BLE001
        notes.append("The auction catalogue could not be read this run.")
        log.warning("home_auctions_unreadable", error=str(exc)[:160])

    picks = marketplace_picks(market) + auction_picks(auctions, now=now)
    picks.sort(key=lambda p: p.multiple, reverse=True)
    picks = picks[:limit]

    live_market = sum(
        1 for e in market.listings.values() if e.state == "live" and e.appraisal
    )
    live_auction = sum(
        1 for e in auctions.lots.values() if e.state in ("live", "ending") and e.watch
    )

    docs.mkdir(parents=True, exist_ok=True)
    page = docs / "index.html"
    page.write_text(
        render_home(
            picks,
            generated_at=f"updated {now.strftime('%b %d, %Y · %H:%M UTC')}",
            marketplace_total=live_market,
            auction_total=live_auction,
            notes=notes,
        ),
        encoding="utf-8",
    )
    return page


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the combined landing page")
    ap.add_argument("--out", default="docs")
    ap.add_argument("--limit", type=int, default=24)
    args = ap.parse_args(argv)
    page = build(Path(args.out), limit=args.limit)
    print(f"wrote {page}")
    return 0


_CSS = """
:root{
  --paper:#f4efe4; --card:#fbf7ec; --ink:#2b2118; --soft:#6b5c49; --line:#e0d5c2;
  --accent:#a8431a; --teal:#2f6d62; --good:#2f6d62; --warn:#8a5a10; --warn-bg:#f3e6c8;
  --crit:#9a3524;
  --shadow:0 1px 0 #fff9, 0 2px 12px #6b5c4922;
  --display:"Iowan Old Style","Palatino Nova",Palatino,"Book Antiqua",Georgia,serif;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono","Roboto Mono",Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#211a13; --card:#2c241b; --ink:#f2e8d8; --soft:#b3a48d; --line:#4a3e2f;
  --accent:#e8804c; --teal:#6fbfae; --good:#6fbfae; --warn:#e0b45e; --warn-bg:#3d3016;
  --crit:#e08a72;
  --shadow:0 1px 0 #ffffff0a, 0 3px 16px #00000059;}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  line-height:1.45;-webkit-font-smoothing:antialiased}
nav.tabs{display:flex;gap:2px;background:var(--card);border-bottom:1px solid var(--line);
  padding:0 20px;overflow-x:auto;position:sticky;top:0;z-index:5}
nav.tabs a{padding:14px 18px;color:var(--soft);text-decoration:none;font-size:14px;
  font-weight:600;white-space:nowrap;border-bottom:2px solid transparent}
nav.tabs a.on{color:var(--accent);border-bottom-color:var(--accent)}
nav.tabs a:hover{color:var(--ink)}
header.top,main,footer{max-width:1200px;margin:0 auto;padding:0 20px}
header.top{padding-top:24px}
.brandrow{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-family:var(--display);font-size:38px;margin:0;font-weight:600}
.tag{font-size:12px;text-transform:uppercase;letter-spacing:.14em;color:var(--teal);
  border:1.5px solid var(--teal);border-radius:999px;padding:4px 12px}
.sub{color:var(--soft);font-size:13px}
.note{max-width:72ch;line-height:1.6}
.notice{font-size:14px;background:var(--warn-bg);color:var(--warn);border:1px solid var(--warn);
  border-radius:10px;padding:10px 14px;margin:12px 0 0}
.summary{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:10px 16px;box-shadow:var(--shadow);min-width:96px;cursor:pointer;
  font:inherit;color:inherit;text-align:left}
.stat:hover{border-color:var(--accent)}
.stat[aria-pressed="true"]{border-color:var(--accent);background:var(--warn-bg)}
.stat .n{font-family:var(--display);font-size:26px;font-weight:600}
.stat .l{font-size:11px;color:var(--soft);text-transform:uppercase;letter-spacing:.09em}
.stat.killer .n{color:var(--accent)}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:14px 0 0}
.chip{font:inherit;font-size:13.5px;min-height:40px;padding:8px 14px;border-radius:999px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
#q{font:inherit;font-size:14px;min-height:40px;padding:8px 14px;border-radius:999px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);min-width:220px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;
  margin-top:26px}
.pick{background:var(--card);border:1px solid var(--line);border-radius:14px;
  overflow:hidden;box-shadow:var(--shadow);display:flex;flex-direction:column;
  text-decoration:none;color:inherit;transition:transform .12s ease,border-color .12s ease}
.pick:hover{transform:translateY(-2px);border-color:var(--accent)}
.media{position:relative;aspect-ratio:4/3;background:var(--line)}
.media img{width:100%;height:100%;object-fit:cover;display:block}
.nophoto{display:flex;align-items:center;justify-content:center;height:100%;
  color:var(--soft);font-size:13px}
.src{position:absolute;top:10px;left:10px;font-size:10.5px;font-weight:700;
  letter-spacing:.09em;text-transform:uppercase;padding:4px 10px;border-radius:999px;
  background:var(--card);color:var(--soft);border:1px solid var(--line)}
.src.auction{background:var(--teal);color:#fff;border-color:var(--teal)}
.src.marketplace{background:var(--accent);color:#fff;border-color:var(--accent)}
.body{padding:14px 16px 16px;display:flex;flex-direction:column;gap:5px}
h3{font-size:15.5px;margin:0;font-weight:650;line-height:1.35}
.clockrow{display:flex;gap:6px;flex-wrap:wrap;align-items:center;min-height:18px}
.clock{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--accent)}
.badge{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--soft);
  border:1px solid var(--line);border-radius:999px;padding:2px 8px}
.row{display:flex;justify-content:space-between;font-size:13.5px;gap:12px}
.row span:first-child{color:var(--soft)}
.v{font-family:var(--mono);font-variant-numeric:tabular-nums}
.good{color:var(--good);font-weight:650}
.why{font-size:12.5px;color:var(--soft);margin:8px 0 0;line-height:1.5}
.empty{color:var(--soft);font-size:14px;margin-top:24px}
footer{padding:44px 20px;color:var(--soft);font-size:14px}
footer a{color:var(--teal);text-decoration:none;font-weight:600}
"""

_JS = r"""
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

var state = {source: 'all', q: ''};
function apply(){
  var shown = 0;
  document.querySelectorAll('.pick').forEach(function(card){
    var okSrc = state.source === 'all' || card.dataset.source === state.source;
    var okText = !state.q || (card.dataset.hay || '').indexOf(state.q) !== -1;
    var show = okSrc && okText;
    card.hidden = !show;
    if (show) shown++;
  });
  var none = document.getElementById('noresults');
  if (none) none.hidden = shown !== 0;
  document.querySelectorAll('.stat[data-source]').forEach(function(b){
    b.setAttribute('aria-pressed', String(b.dataset.source === state.source));
  });
}
document.querySelectorAll('.stat[data-source]').forEach(function(btn){
  btn.addEventListener('click', function(){
    var v = btn.dataset.source;
    state.source = (state.source === v) ? 'all' : v;
    apply();
  });
});
var q = document.getElementById('q');
if (q) q.addEventListener('input', function(){
  state.q = q.value.trim().toLowerCase(); apply();
});
var reset = document.getElementById('reset');
if (reset) reset.addEventListener('click', function(){
  state = {source:'all', q:''}; if (q) q.value = ''; apply();
});
apply();
"""


if __name__ == "__main__":
    raise SystemExit(main())
