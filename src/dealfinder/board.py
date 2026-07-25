"""Render the deal board — the static page GitHub Pages serves to your iPad.

Takes the engine's ranked output and writes a self-contained HTML file (plus local photo
files) into an output directory. No server, no database: the whole site is regenerated each
run and committed, which is what makes hosting free.
"""

from __future__ import annotations

import html
import shutil
from dataclasses import dataclass
from pathlib import Path

from dealfinder.engine import EvaluatedPiece, RunResult
from dealfinder.resale import Posture

_POSTURE_LABEL = {
    Posture.MARKET: "market",
    Posture.KNOWN_PREMIUM: "known · premium",
    Posture.CEILING_TEST: "ceiling test",
}


def _money(cents: int | None) -> str:
    return f"${(cents or 0) / 100:,.0f}"


@dataclass
class BoardMeta:
    title: str = "The Bench"
    region: str = "Lexington · 40 mi"
    generated_at: str = ""
    note: str = ""


def _resale_row(p: EvaluatedPiece) -> str:
    """The sell-side line.

    Always shows the target price — even on a thin piece, since knowing what it fetches is
    the point — and states *why* when the economics are marginal. Showing a bare "skip"
    next to a positive margin reads as a broken card.
    """
    reason = (
        f'<div class="reason">{html.escape(p.resale.warning)}</div>'
        if p.resale.warning
        else ""
    )
    if p.resale.status == "underwater":
        return (
            f'<div class="resale bad"><b>Don\'t buy at this price</b>'
            f'<span class="posture bad">loses money</span></div>{reason}'
        )
    if p.resale.status == "thin":
        return (
            f'<div class="resale">Sell target <b>{_money(p.resale.list_price_cents)}</b>'
            f'<span class="posture thin">thin for the hours</span></div>{reason}'
        )
    return (
        f'<div class="resale">Sell target <b>{_money(p.resale.list_price_cents)}</b>'
        f'<span class="posture">{_POSTURE_LABEL[p.resale.posture]}</span></div>'
    )


def _card(rank: int, p: EvaluatedPiece, photo_rel: str | None) -> str:
    listing = p.listing
    chips = "".join(
        f'<span class="chip {b.tone}"><span class="ico">{html.escape(b.icon)}</span>'
        f"{html.escape(b.label)}</span>"
        for b in p.badges
    )
    warn = (
        f'<p class="authwarn">⚠ {html.escape(p.authenticity.warnings[0])}</p>'
        if p.authenticity.warnings
        else ""
    )
    was_cents = listing.raw_json.get("_was_price_cents")
    was = f'<span class="was">{_money(was_cents)}</span>' if was_cents else ""
    thumb = (
        f'<img class="thumb" src="{html.escape(photo_rel)}" alt="" loading="lazy">'
        if photo_rel
        else f'<div class="thumb ph" aria-hidden="true"><span>'
        f"{html.escape(p.appraisal.identified_item.title())}</span></div>"
    )
    klass = (
        "card killer"
        if p.is_killer
        else ("card flagged" if p.authenticity.is_red_flag else "card")
    )
    return f"""
    <article class="{klass}">
      <div class="rank">{rank}</div>
      {thumb}
      <div class="body">
        <div class="head">
          <h2>{html.escape(listing.title) or "Untitled"}</h2>
          <div class="loc">{html.escape(listing.location_text)}</div>
        </div>
        <div class="chips">{chips}</div>
        {warn}
        <div class="figs">
          <div class="fig"><span class="k">Ask</span><span class="v">{_money(listing.asking_price_cents)} {was}</span></div>
          <div class="fig"><span class="k">Est. resale</span><span class="v">{_money(p.appraisal.est_restored_resale_value_cents)}</span></div>
          <div class="fig net"><span class="k">Est. margin</span><span class="v">{_money(p.cash_margin_cents)}</span></div>
        </div>
        {_resale_row(p)}
        <div class="meters">
          <div class="meter"><label>Priority</label><div class="bar"><i style="width:{p.priority}%"></i></div><b>{p.priority:.0f}</b></div>
          <div class="meter"><label>Sells</label><div class="bar sub"><i style="width:{p.liquidity}%"></i></div><b>{p.liquidity:.0f}</b></div>
          <div class="meter"><label>Heat</label><div class="bar sub"><i style="width:{p.heat}%"></i></div><b>{p.heat:.0f}</b></div>
        </div>
        <details class="why"><summary>Why</summary><p>{html.escape(p.appraisal.reasoning[:600])}</p></details>
        <a class="view" href="{html.escape(listing.url)}" target="_blank" rel="noopener">View listing →</a>
      </div>
    </article>"""


def render_board(
    result: RunResult,
    *,
    meta: BoardMeta | None = None,
    photo_map: dict[str, str] | None = None,
) -> str:
    """Return the complete HTML page for a run. ``photo_map`` maps listing id -> relative path."""
    meta = meta or BoardMeta()
    photo_map = photo_map or {}
    pieces = result.pieces
    cards = "\n".join(
        _card(i + 1, p, photo_map.get(p.listing.fb_listing_id))
        for i, p in enumerate(pieces)
    )
    plan = result.plan
    stats = {
        "N": len(pieces),
        "KILLERS": sum(1 for p in pieces if p.is_killer),
        "DROPS": sum(1 for p in pieces if p.price_dropped),
        "FAKES": sum(1 for p in pieces if p.authenticity.is_red_flag),
        "INR": sum(1 for p in pieces if not p.out_of_radius),
    }
    page = _TEMPLATE
    for key, val in stats.items():
        page = page.replace("{{" + key + "}}", str(val))
    return (
        page.replace("{{CARDS}}", cards)
        .replace("{{TITLE}}", html.escape(meta.title))
        .replace("{{REGION}}", html.escape(meta.region))
        .replace("{{GENERATED}}", html.escape(meta.generated_at))
        .replace("{{FUNNEL}}", html.escape(plan.summary()))
        .replace("{{NOTE}}", html.escape(meta.note))
    )


def write_site(
    result: RunResult,
    out_dir: Path,
    *,
    meta: BoardMeta | None = None,
    photo_files: dict[str, Path] | None = None,
    extra_photo_map: dict[str, str] | None = None,
) -> Path:
    """Write index.html (and copy photos) into ``out_dir``; return the page path.

    ``extra_photo_map`` carries already-committed photos for pieces appraised on earlier
    runs, so they render without being copied again.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    photo_map: dict[str, str] = dict(extra_photo_map or {})
    if photo_files:
        pdir = out_dir / "photos"
        pdir.mkdir(exist_ok=True)
        for listing_id, src in photo_files.items():
            src = Path(src)
            if not src.exists():
                continue
            dest = pdir / f"{listing_id}{src.suffix or '.jpg'}"
            # A catalogue photo handed back as its own source would raise SameFileError —
            # and would do so on the *second* run, not the first.
            if src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
            photo_map[listing_id] = f"photos/{dest.name}"
    page = out_dir / "index.html"
    page.write_text(render_board(result, meta=meta, photo_map=photo_map))
    return page


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="The Bench">
<meta name="theme-color" content="#e7e4dc" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#181712" media="(prefers-color-scheme:dark)">
<title>{{TITLE}} — deal board</title>
<style>
:root{--paper:#e7e4dc;--card:#f2efe8;--ink:#26241f;--soft:#6a655a;--line:#d3cfc4;
  --brass:#2f7d6b;--brass-soft:#e0ebe6;--good:#2f7d5a;--warn:#9a6b1e;--warn-bg:#f3e6cd;
  --crit:#8f3a2f;--crit-bg:#f2ddd7;--star:#b98a1f;--shadow:0 1px 0 #fff8,0 2px 10px #0000000f}
@media (prefers-color-scheme:dark){:root{--paper:#181712;--card:#22201b;--ink:#ece8de;
  --soft:#9a9384;--line:#332f27;--brass:#4fb39a;--brass-soft:#12352e;--good:#4fb38a;
  --warn:#d0a24e;--warn-bg:#33280f;--crit:#d98069;--crit-bg:#37201a;--star:#e0b34e;
  --shadow:0 1px 0 #ffffff08,0 3px 14px #0000004d}}
:root[data-theme="light"]{--paper:#e7e4dc;--card:#f2efe8;--ink:#26241f;--soft:#6a655a;
  --line:#d3cfc4;--brass:#2f7d6b;--brass-soft:#e0ebe6;--good:#2f7d5a;--warn:#9a6b1e;
  --warn-bg:#f3e6cd;--crit:#8f3a2f;--crit-bg:#f2ddd7;--star:#b98a1f;--shadow:0 1px 0 #fff8,0 2px 10px #0000000f}
:root[data-theme="dark"]{--paper:#181712;--card:#22201b;--ink:#ece8de;--soft:#9a9384;
  --line:#332f27;--brass:#4fb39a;--brass-soft:#12352e;--good:#4fb38a;--warn:#d0a24e;
  --warn-bg:#33280f;--crit:#d98069;--crit-bg:#37201a;--star:#e0b34e;--shadow:0 1px 0 #ffffff08,0 3px 14px #0000004d}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.4}
.num,.v,.was,.rank,.meter b,.resale b{font-variant-numeric:tabular-nums;
  font-family:ui-monospace,"SF Mono","Roboto Mono",Menlo,monospace}
header.top{padding:26px 18px 14px;max-width:1120px;margin:0 auto}
.brandrow{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h1{font-size:26px;margin:0;letter-spacing:-.02em;font-weight:650}
.sub{color:var(--soft);font-size:12.5px}
.tag{font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--brass);
  border:1px solid var(--line);border-radius:999px;padding:3px 10px}
.summary{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:9px 13px;box-shadow:var(--shadow);min-width:90px}
.stat .n{font-size:21px;font-weight:650;font-variant-numeric:tabular-nums}
.stat .l{font-size:10.5px;color:var(--soft);text-transform:uppercase;letter-spacing:.1em}
.stat.killer .n{color:var(--star)} .stat.fake .n{color:var(--crit)}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin:14px auto 0;max-width:1120px;padding:0 18px}
.controls button{background:var(--card);border:1px solid var(--line);color:var(--ink);
  border-radius:999px;padding:8px 14px;font-size:13px;cursor:pointer}
.controls button[aria-pressed="true"]{background:var(--brass);border-color:var(--brass);color:#fff}
:root[data-theme="dark"] .controls button[aria-pressed="true"]{color:#0c1a16}
.legend{max-width:1120px;margin:11px auto 0;padding:0 18px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.legend .legend-note{font-size:10.5px;color:var(--soft);text-transform:uppercase;letter-spacing:.1em}
.grid{max-width:1120px;margin:16px auto 50px;padding:0 18px;display:grid;
  grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;align-items:start}
.card{position:relative;display:grid;grid-template-columns:104px 1fr;
  background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow:var(--shadow)}
.card.killer{border-color:var(--star);box-shadow:0 0 0 1px var(--star),var(--shadow)}
.card.flagged{border-left:4px solid var(--crit)}
.rank{position:absolute;top:8px;left:8px;z-index:2;background:#0007;color:#fff;
  font-size:11px;padding:2px 7px;border-radius:999px}
img.thumb{width:104px;height:100%;min-height:150px;object-fit:cover;display:block;background:var(--brass-soft)}
.thumb.ph{background:repeating-linear-gradient(115deg,#0000 0 7px,#00000008 7px 8px),
  linear-gradient(160deg,var(--brass-soft),var(--card));display:flex;align-items:center;
  justify-content:center;padding:6px;text-align:center}
.thumb.ph span{font-size:12px;font-weight:600;color:var(--brass);opacity:.85;
  text-transform:uppercase;letter-spacing:.05em;line-height:1.15}
.body{padding:12px 14px 14px;min-width:0}
.head h2{font-size:15px;margin:0 0 2px;line-height:1.25;font-weight:600;overflow:hidden;
  text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.loc{font-size:12px;color:var(--soft)}
.chips{display:flex;gap:5px;flex-wrap:wrap;margin:8px 0}
.chip{font-size:11px;padding:2px 8px 2px 6px;border-radius:999px;border:1px solid var(--line);
  display:inline-flex;align-items:center;gap:4px;background:var(--paper)}
.chip.good{color:var(--good);border-color:var(--good)}
.chip.warn{color:var(--crit);border-color:var(--crit);background:var(--crit-bg)}
.chip.info{color:var(--brass)}
.authwarn{font-size:11.5px;color:var(--crit);background:var(--crit-bg);border-radius:8px;padding:6px 9px;margin:6px 0 2px}
.figs{display:flex;gap:14px;margin:10px 0 4px;border-top:1px solid var(--line);padding-top:10px;flex-wrap:wrap}
.fig{display:flex;flex-direction:column}
.fig .k{font-size:10.5px;color:var(--soft);text-transform:uppercase;letter-spacing:.08em}
.fig .v{font-size:15px;font-weight:600}
.fig.net .v{color:var(--good)}
.was{font-size:11px;color:var(--soft);text-decoration:line-through;font-weight:400}
.resale{font-size:12px;color:var(--soft);margin:2px 0 8px;display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.resale b{color:var(--ink);font-size:13px}
.resale .posture{font-size:10px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--brass);border:1px solid var(--line);border-radius:999px;padding:1px 7px}
.resale.bad b{color:var(--crit)}
.resale .posture.bad{color:var(--crit);border-color:var(--crit);background:var(--crit-bg)}
.resale .posture.thin{color:var(--warn);border-color:var(--warn);background:var(--warn-bg)}
.reason{font-size:11.5px;color:var(--soft);margin:-4px 0 8px;line-height:1.4}
.meters{display:flex;flex-direction:column;gap:5px;margin:8px 0 10px}
.meter{display:grid;grid-template-columns:52px 1fr 26px;align-items:center;gap:8px}
.meter label{font-size:10.5px;color:var(--soft);text-transform:uppercase;letter-spacing:.06em}
.meter .bar{height:6px;background:var(--paper);border-radius:999px;overflow:hidden;border:1px solid var(--line)}
.meter .bar i{display:block;height:100%;background:var(--brass);border-radius:999px}
.meter .bar.sub i{background:var(--soft);opacity:.6}
.meter b{font-size:12px;text-align:right}
.why{margin:0 0 9px}
.why summary{font-size:11.5px;color:var(--soft);cursor:pointer;list-style:none}
.why summary::-webkit-details-marker{display:none}
.why summary::before{content:"▸ ";color:var(--brass)}
.why[open] summary::before{content:"▾ "}
.why p{font-size:12px;color:var(--soft);margin:6px 0 0;line-height:1.45}
.view{display:inline-block;font-size:13.5px;color:var(--brass);text-decoration:none;font-weight:600}
.foot{max-width:1120px;margin:0 auto 46px;padding:0 18px;color:var(--soft);font-size:11.5px;line-height:1.6}
.hide{display:none!important}
@media (max-width:420px){.card{grid-template-columns:84px 1fr}img.thumb{width:84px}}
</style>
</head>
<body>
<header class="top">
  <div class="brandrow">
    <h1>{{TITLE}}</h1>
    <span class="tag">{{REGION}}</span>
    <span class="sub">{{GENERATED}}</span>
  </div>
  <div class="summary">
    <div class="stat"><div class="n num">{{N}}</div><div class="l">Candidates</div></div>
    <div class="stat killer"><div class="n num">{{KILLERS}}</div><div class="l">★ Killers</div></div>
    <div class="stat"><div class="n num">{{DROPS}}</div><div class="l">▼ Price drops</div></div>
    <div class="stat fake"><div class="n num">{{FAKES}}</div><div class="l">⚠ Look-alikes</div></div>
    <div class="stat"><div class="n num">{{INR}}</div><div class="l">In radius</div></div>
  </div>
</header>
<div class="controls">
  <button id="f-all" aria-pressed="true">All</button>
  <button id="f-radius" aria-pressed="false">In radius only</button>
  <button id="f-clean" aria-pressed="false">Hide look-alikes</button>
  <button id="f-star" aria-pressed="false">★ Killers only</button>
</div>
<div class="legend">
  <span class="chip good"><span class="ico">★</span>Killer deal</span>
  <span class="chip warn"><span class="ico">⚠</span>Look-alike</span>
  <span class="chip good"><span class="ico">▼</span>Price drop</span>
  <span class="chip info"><span class="ico">◉</span>Hot</span>
  <span class="chip info"><span class="ico">≈</span>Sells fast</span>
  <span class="chip warn"><span class="ico">⤢</span>Out of radius</span>
  <span class="legend-note">what the flags mean</span>
</div>
<main class="grid" id="grid">
{{CARDS}}
</main>
<p class="foot">{{FUNNEL}}<br>{{NOTE}}</p>
<script>
const grid=document.getElementById('grid');
const cards=[...grid.children];
const btns=['f-all','f-radius','f-clean','f-star'];
function apply(mode){
  btns.forEach(x=>document.getElementById(x).setAttribute('aria-pressed',String(x===mode)));
  cards.forEach(c=>{
    const oor=[...c.querySelectorAll('.chip.warn')].some(e=>e.textContent.includes('Out of radius'));
    const fake=c.classList.contains('flagged');
    const star=c.classList.contains('killer');
    let show=true;
    if(mode==='f-radius'&&oor)show=false;
    if(mode==='f-clean'&&fake)show=false;
    if(mode==='f-star'&&!star)show=false;
    c.classList.toggle('hide',!show);
  });
}
btns.forEach(id=>document.getElementById(id).addEventListener('click',()=>apply(id)));
</script>
</body>
</html>
"""
