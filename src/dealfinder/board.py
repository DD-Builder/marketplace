"""Render the deal board — the static page GitHub Pages serves to your iPad.

Takes the engine's ranked output and writes a self-contained HTML file (plus local photo
files) into an output directory. No server, no database: the whole site is regenerated each
run and committed, which is what makes hosting free.
"""

from __future__ import annotations

import html
import json
import re
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


def _seen_label(days: float) -> str:
    """How old the evidence is that this piece is still for sale."""
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    return f"{int(days)} days ago"


def _seen_tone(days: float) -> str:
    return "bad" if days >= 7 else ("warn" if days >= 2 else "")


def _short_label(identified_item: str, limit: int = 42) -> str:
    """A thumbnail-sized name for a piece with no photo.

    ``identified_item`` comes back from a real appraisal as a descriptive sentence —
    "mixed furniture lot: 5-drawer light-wood dresser, dark-wood 2-drawer media console
    with open shelving, and ..." — so take the first clause and cut it short.
    """
    # The hyphen is deliberately not a separator: "three-piece bedroom set" is one clause,
    # and splitting on it left cards labelled just "Three".
    head = re.split(r"[:;(\u2014]|,\s", identified_item.strip(), maxsplit=1)[0].strip()
    head = head or identified_item.strip() or "no photo"
    if len(head) > limit:
        head = head[:limit].rsplit(" ", 1)[0] + "\u2026"
    return head.title()


@dataclass
class BoardMeta:
    title: str = "The Bench"
    region: str = "Lexington · 40 mi"
    generated_at: str = ""
    note: str = ""
    # Where the page sends its writes. Empty ``repo`` renders a read-only board — every
    # button explains that it isn't wired up rather than failing silently.
    repo: str = ""                              # "owner/repo"
    branch: str = "main"
    board_workflow: str = "deal-board.yml"
    negotiate_workflow: str = "negotiate.yml"
    pieces_path: str = "docs/pieces.json"
    drafts_dir: str = ".drafts"


def _your_numbers(p: EvaluatedPiece) -> str:
    """The second tier: the same piece measured against your books.

    Folded away by default. The market number is what a buyer will pay and shouldn't move
    because you spent a long weekend on it; these are the numbers that tell you whether the
    weekend was worth it.
    """
    y = p.resale.yours
    hours = y.costs.labor_hours
    wage = y.projected.effective_hourly_cents
    basis = "your logged costs" if y.logged else "estimated: bought at ask, restored per estimate"
    rows = [
        ("Cost basis", _money(y.cash_outlay_cents)),
        ("With your time", f"{_money(y.loaded_cost_cents)} · {hours:.0f}h"),
        ("Profit at target", _money(y.projected.cash_profit_cents)),
        ("Your rate", f"{_money(wage)}/hr" if wage is not None else "—"),
        ("Walk away below", _money(y.floor_price_cents)),
    ]
    cells = "".join(
        f'<div class="yn"><span class="k">{html.escape(k)}</span>'
        f'<span class="v">{html.escape(v)}</span></div>'
        for k, v in rows
    )
    # No warning here: _resale_row already prints it directly above, and showing the same
    # sentence twice on one card reads as a rendering bug.
    note = f'<p class="basis">{html.escape(basis)}</p>'
    return (
        '<details class="yours"><summary>Your numbers</summary>'
        f'<div class="yngrid">{cells}</div>{note}</details>'
    )


def _resale_row(p: EvaluatedPiece) -> str:
    """The sell-side line: the market's number, headline.

    Always shows the target price — even on a thin piece, since knowing what it fetches is
    the point — and states *why* when the economics are marginal. Showing a bare "skip"
    next to a positive margin reads as a broken card.
    """
    market, yours = p.resale.market, p.resale.yours
    low, high = p.resale.range_cents
    span = f'<span class="span">{_money(low)}–{_money(high)}</span>' if low != high else ""
    reason = (
        f'<div class="reason">{html.escape(yours.warning)}</div>' if yours.warning else ""
    )
    if yours.status == "underwater":
        head = (
            f'<div class="resale bad"><b>Don\'t buy at this price</b>'
            f'<span class="posture bad">loses money</span></div>'
        )
    elif yours.status == "thin":
        head = (
            f'<div class="resale">Sell target <b>{_money(p.resale.headline_cents)}</b>{span}'
            f'<span class="posture thin">thin for the hours</span></div>'
        )
    else:
        head = (
            f'<div class="resale">Sell target <b>{_money(p.resale.headline_cents)}</b>{span}'
            f'<span class="posture">{_POSTURE_LABEL[market.posture]}</span></div>'
        )
    return head + reason + _your_numbers(p)


def _tools(p: EvaluatedPiece) -> str:
    """The two write-side panels on every card: your books, and the negotiation drafter.

    Both are plain forms wired to the GitHub API from the browser. Folded shut by default
    so the board still reads as a board.
    """
    lid = html.escape(p.listing.fb_listing_id)
    ask = p.listing.asking_price_cents or 0
    return f"""
        <details class="tool log"><summary>Log this piece</summary>
          <form class="logform" data-id="{lid}">
            <label>Paid <input name="paid" type="number" inputmode="decimal" step="1"
              placeholder="{ask / 100:.0f}"></label>
            <label>Materials <input name="materials" type="number" inputmode="decimal"
              step="1" placeholder="0"></label>
            <label>Hours <input name="hours" type="number" inputmode="decimal" step="0.5"
              placeholder="0"></label>
            <label>Sold for <input name="sold" type="number" inputmode="decimal" step="1"
              placeholder="—"></label>
            <button type="submit">Save to my books</button>
            <p class="status" role="status"></p>
          </form>
        </details>
        <details class="tool nego"><summary>Draft a message to the seller</summary>
          <form class="negoform" data-id="{lid}">
            <label class="wide">Posture
              <input name="posture" type="range" min="0" max="100" step="5" value="40">
              <span class="postval">measured</span>
            </label>
            <label class="wide">Their messages so far
              <textarea name="conversation" rows="3"
                placeholder="Paste the thread — leave empty for an opener"></textarea></label>
            <label class="wide">Leverage / flaws
              <textarea name="notes" rows="2"
                placeholder="Water ring on the top, one drawer runner broken…"></textarea></label>
            <button type="submit">Draft replies</button>
            <p class="status" role="status"></p>
            <div class="drafts"></div>
            <p class="basis">Drafts only — nothing is ever sent for you.</p>
          </form>
        </details>"""


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
        f"{html.escape(_short_label(p.appraisal.identified_item))}</span></div>"
    )
    klass = (
        "card killer"
        if p.is_killer
        else ("card flagged" if p.authenticity.is_red_flag else "card")
    )
    return f"""
    <article class="{klass}" data-id="{html.escape(listing.fb_listing_id)}">
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
          <div class="fig"><span class="k">Last confirmed</span><span class="v {_seen_tone(p.days_since_seen)}">{_seen_label(p.days_since_seen)}</span></div>
        </div>
        {_resale_row(p)}
        <div class="meters">
          <div class="meter"><label>Priority</label><div class="bar"><i style="width:{p.priority}%"></i></div><b>{p.priority:.0f}</b></div>
          <div class="meter"><label>Sells</label><div class="bar sub"><i style="width:{p.liquidity}%"></i></div><b>{p.liquidity:.0f}</b></div>
          <div class="meter"><label>Heat</label><div class="bar sub"><i style="width:{p.heat}%"></i></div><b>{p.heat:.0f}</b></div>
        </div>
        <details class="why"><summary>Why</summary><p>{html.escape(p.appraisal.reasoning[:600])}</p></details>
{_tools(p)}
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
    # Config for the page's write side. json.dumps also escapes it safely for a <script>
    # block; </script> can't appear in any of these values, but the escape is free.
    config = json.dumps(
        {
            "repo": meta.repo,
            "branch": meta.branch,
            "boardWorkflow": meta.board_workflow,
            "negotiateWorkflow": meta.negotiate_workflow,
            "piecesPath": meta.pieces_path,
            "draftsDir": meta.drafts_dir,
        }
    ).replace("</", "<\\/")

    page = _TEMPLATE
    for key, val in stats.items():
        page = page.replace("{{" + key + "}}", str(val))
    return (
        page.replace("{{CARDS}}", cards)
        .replace("{{CONFIG}}", config)
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
  justify-content:center;padding:6px;text-align:center;overflow:hidden}
/* A real appraisal names the piece in a full sentence, so this has to clip hard —
   unclipped it spilled out of the 104px column and across the whole card. */
.thumb.ph span{font-size:12px;font-weight:600;color:var(--brass);opacity:.85;
  text-transform:uppercase;letter-spacing:.05em;line-height:1.15;overflow:hidden;
  overflow-wrap:break-word;display:-webkit-box;-webkit-line-clamp:6;-webkit-box-orient:vertical}
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
.fig .v.warn{color:var(--warn)}
.fig .v.bad{color:var(--crit)}
.was{font-size:11px;color:var(--soft);text-decoration:line-through;font-weight:400}
.resale{font-size:12px;color:var(--soft);margin:2px 0 8px;display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.resale b{color:var(--ink);font-size:13px}
.resale .posture{font-size:10px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--brass);border:1px solid var(--line);border-radius:999px;padding:1px 7px}
.resale.bad b{color:var(--crit)}
.resale .posture.bad{color:var(--crit);border-color:var(--crit);background:var(--crit-bg)}
.resale .posture.thin{color:var(--warn);border-color:var(--warn);background:var(--warn-bg)}
.reason{font-size:11.5px;color:var(--soft);margin:-4px 0 8px;line-height:1.4}
.resale .span{font-size:11px;color:var(--soft)}
.yours{margin:-2px 0 9px}
.yours summary{font-size:11.5px;color:var(--soft);cursor:pointer;list-style:none}
.yours summary::-webkit-details-marker{display:none}
.yours summary::before{content:"▸ ";color:var(--brass)}
.yours[open] summary::before{content:"▾ "}
.yngrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:5px 12px;
  margin:7px 0 0;padding:8px 10px;background:var(--paper);border:1px solid var(--line);border-radius:8px}
.yn{display:flex;flex-direction:column;gap:1px}
.yn .k{font-size:9.5px;color:var(--soft);text-transform:uppercase;letter-spacing:.06em}
.yn .v{font-size:12.5px;color:var(--ink);font-variant-numeric:tabular-nums}
.basis{font-size:10.5px;color:var(--soft);margin:6px 0 0;font-style:italic}
.actionbar{max-width:1120px;margin:0 auto 8px;padding:0 18px;display:flex;gap:8px;
  align-items:center;flex-wrap:wrap}
.actionbar button{font:inherit;font-size:13px;padding:7px 14px;border-radius:999px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.actionbar button.primary{background:var(--brass);border-color:var(--brass);color:var(--paper);
  font-weight:600}
.conn{font-size:11.5px;color:var(--soft)}
.conn.ok{color:var(--good)}
.conn.bad{color:var(--crit)}
.tool{margin:0 0 7px}
.tool summary{font-size:11.5px;color:var(--soft);cursor:pointer;list-style:none}
.tool summary::-webkit-details-marker{display:none}
.tool summary::before{content:"▸ ";color:var(--brass)}
.tool[open] summary::before{content:"▾ "}
.tool form{display:flex;flex-wrap:wrap;gap:7px 10px;margin:8px 0 0;padding:9px 10px;
  background:var(--paper);border:1px solid var(--line);border-radius:8px}
.tool label{display:flex;flex-direction:column;gap:2px;font-size:9.5px;color:var(--soft);
  text-transform:uppercase;letter-spacing:.06em;flex:1 1 88px}
.tool label.wide{flex:1 1 100%;text-transform:none;letter-spacing:0;font-size:10.5px}
.tool input,.tool textarea{font:inherit;font-size:13px;padding:5px 7px;border-radius:6px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);width:100%;
  box-sizing:border-box}
.tool input[type=range]{padding:0;accent-color:var(--brass)}
.tool button{font:inherit;font-size:12.5px;padding:6px 13px;border-radius:999px;
  border:1px solid var(--brass);background:var(--brass-soft);color:var(--brass);
  cursor:pointer;flex:0 0 auto;align-self:flex-end}
.postval{font-size:11px;color:var(--brass);text-transform:none;letter-spacing:0}
.status{flex:1 1 100%;font-size:11.5px;color:var(--soft);margin:2px 0 0;min-height:1em}
.status.ok{color:var(--good)}
.status.bad{color:var(--crit)}
.drafts{flex:1 1 100%;display:flex;flex-direction:column;gap:8px}
.draft{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 11px}
.dtext{font-size:13px;line-height:1.5;margin:0;white-space:pre-wrap}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);
  max-width:min(440px,92vw);padding:18px 20px;box-shadow:var(--shadow)}
dialog::backdrop{background:#0009}
dialog h2{font-size:16px;margin:0 0 8px}
dialog p{font-size:12px;color:var(--soft);line-height:1.5;margin:0 0 10px}
dialog code{font-size:11.5px;color:var(--brass)}
dialog label{display:flex;flex-direction:column;gap:3px;font-size:10px;color:var(--soft);
  text-transform:uppercase;letter-spacing:.06em;margin:0 0 8px}
dialog input{font:inherit;font-size:13.5px;padding:7px 9px;border-radius:6px;
  border:1px solid var(--line);background:var(--paper);color:var(--ink)}
dialog menu{display:flex;gap:8px;padding:0;margin:12px 0 0}
dialog menu button{font:inherit;font-size:13px;padding:6px 14px;border-radius:999px;
  border:1px solid var(--line);background:var(--paper);color:var(--ink);cursor:pointer}
dialog menu button:first-child{background:var(--brass);border-color:var(--brass);
  color:var(--card);font-weight:600}
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
<div class="actionbar">
  <button id="scrape-now" class="primary">Scrape now</button>
  <button id="open-settings">Connection</button>
  <span id="conn" class="conn">not connected</span>
</div>
<dialog id="settings">
  <form method="dialog" class="settings">
    <h2>Connect this page to your repo</h2>
    <p>The board is a static page, so the buttons talk to GitHub directly. Paste a
      <b>fine-grained personal access token</b> scoped to <code id="repo-name"></code>
      with <b>Actions: read &amp; write</b> and <b>Contents: read &amp; write</b>.</p>
    <label>Token <input id="token" type="password" autocomplete="off"
      placeholder="github_pat_…"></label>
    <p class="basis">Stored only in this browser. Anyone with your unlocked device could
      use it — revoke it in GitHub settings in one click if that ever matters.</p>
    <menu>
      <button id="save-token" value="save">Save</button>
      <button id="forget-token" value="forget" formnovalidate>Forget</button>
      <button value="cancel" formnovalidate>Close</button>
    </menu>
    <p id="settings-status" class="status" role="status"></p>
  </form>
</dialog>
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
<script>
// ---- the write side -------------------------------------------------------------------
// The page is static, so every action here is a direct call to GitHub's API with a token
// you paste once. No server, no backend, nothing to pay for. Every failure path ends in a
// sentence you can act on — a button that silently does nothing is the worst outcome.
const CFG = {{CONFIG}};
const S = {
  get token(){ return localStorage.getItem('bench_token') || ''; },
  set token(v){ v ? localStorage.setItem('bench_token', v) : localStorage.removeItem('bench_token'); }
};
const $ = s => document.querySelector(s);
const b64 = str => {
  const bytes = new TextEncoder().encode(str);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000)
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
};
const unb64 = s => new TextDecoder().decode(
  Uint8Array.from(atob(s.replace(/\s/g, '')), c => c.charCodeAt(0)));

function explain(status, body){
  const msg = (body && body.message) || '';
  if (status === 401) return 'GitHub rejected the token. Paste a fresh one under Connection.';
  if (status === 403) return 'Token lacks permission. It needs Actions: read & write and '
    + 'Contents: read & write on this repo.';
  if (status === 404) return 'Not found — usually the token isn\'t scoped to ' + CFG.repo
    + ', or the workflow file is missing on ' + CFG.branch + '.';
  if (status === 409 || status === 422) return 'GitHub refused the write (' + (msg || status)
    + '). Reload the page and try once more — someone else may have written first.';
  return 'GitHub returned ' + status + (msg ? ': ' + msg : '') + '.';
}

async function api(path, opts = {}){
  if (!CFG.repo) throw new Error('This board was built without a repo, so the buttons '
    + 'have nothing to talk to. Re-run the pipeline from Actions.');
  if (!S.token) throw new Error('No token yet — tap Connection and paste one.');
  const res = await fetch('https://api.github.com' + path, Object.assign({}, opts, {
    headers: Object.assign({
      'Accept': 'application/vnd.github+json',
      'Authorization': 'Bearer ' + S.token,
      'X-GitHub-Api-Version': '2022-11-28'
    }, opts.headers || {})
  }));
  if (!res.ok){
    let body = null;
    try { body = await res.json(); } catch (e) { /* GitHub sometimes returns no body */ }
    throw new Error(explain(res.status, body));
  }
  return res.status === 204 ? null : res.json();
}

const say = (el, msg, kind) => { if (el){ el.textContent = msg; el.className = 'status ' + (kind || ''); } };

async function checkConnection(){
  const el = $('#conn');
  if (!CFG.repo){ el.textContent = 'read-only board'; el.className = 'conn'; return; }
  if (!S.token){ el.textContent = 'not connected'; el.className = 'conn'; return; }
  try {
    await api('/repos/' + CFG.repo);
    el.textContent = 'connected'; el.className = 'conn ok';
  } catch (err) {
    el.textContent = 'token problem'; el.className = 'conn bad';
    el.title = err.message;
  }
}

// ---- scrape now -----------------------------------------------------------------------
$('#scrape-now').addEventListener('click', async () => {
  const el = $('#conn');
  say(el, 'starting…');
  try {
    await api('/repos/' + CFG.repo + '/actions/workflows/' + CFG.boardWorkflow + '/dispatches', {
      method: 'POST',
      body: JSON.stringify({ ref: CFG.branch, inputs: {} })
    });
    el.textContent = 'scrape started — refresh in a few minutes';
    el.className = 'conn ok';
  } catch (err) { el.textContent = err.message; el.className = 'conn bad'; }
});

// ---- connection dialog ----------------------------------------------------------------
$('#repo-name').textContent = CFG.repo || '(not configured)';
$('#open-settings').addEventListener('click', () => {
  $('#token').value = S.token;
  $('#settings').showModal();
});
$('#save-token').addEventListener('click', () => { S.token = $('#token').value.trim(); setTimeout(checkConnection, 0); });
$('#forget-token').addEventListener('click', () => { S.token = ''; setTimeout(checkConnection, 0); });

// ---- log a piece ----------------------------------------------------------------------
const cents = v => { const n = parseFloat(v); return isNaN(n) ? null : Math.round(n * 100); };

async function readJson(path){
  try {
    const r = await api('/repos/' + CFG.repo + '/contents/' + path + '?ref=' + CFG.branch
      + '&nocache=' + Date.now());
    return { data: JSON.parse(unb64(r.content)), sha: r.sha };
  } catch (err) {
    if (/Not found/.test(err.message)) return { data: null, sha: null };
    throw err;
  }
}

document.querySelectorAll('.logform').forEach(form => {
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const st = form.querySelector('.status');
    const id = form.dataset.id;
    const card = form.closest('article');
    const fd = new FormData(form);
    const paid = cents(fd.get('paid')), materials = cents(fd.get('materials'));
    const hours = parseFloat(fd.get('hours')), sold = cents(fd.get('sold'));
    if (paid === null && materials === null && isNaN(hours) && sold === null){
      say(st, 'Nothing to save — fill in at least one field.', 'bad'); return;
    }
    say(st, 'saving…');
    try {
      const cur = await readJson(CFG.piecesPath);
      const ledger = cur.data || { version: 1, pieces: {} };
      const prev = ledger.pieces[id] || { listing_id: id };
      const entry = Object.assign({}, prev, {
        listing_id: id,
        title: prev.title || (card ? card.querySelector('h2').textContent.trim() : '')
      });
      if (paid !== null) entry.acquired_price_cents = paid;
      if (materials !== null) entry.materials_cents = materials;
      if (!isNaN(hours)) entry.labor_hours = hours;
      if (sold !== null){
        entry.sold_price_cents = sold;
        entry.sold_at = entry.sold_at || new Date().toISOString();
      }
      ledger.pieces[id] = entry;
      const body = { message: 'Log piece ' + id, branch: CFG.branch,
                     content: b64(JSON.stringify(ledger, null, 1)) };
      if (cur.sha) body.sha = cur.sha;
      await api('/repos/' + CFG.repo + '/contents/' + CFG.piecesPath,
                { method: 'PUT', body: JSON.stringify(body) });
      say(st, 'Saved. Your numbers update on the next run.', 'ok');
    } catch (err) { say(st, err.message, 'bad'); }
  });
});

// ---- negotiation ----------------------------------------------------------------------
const POSTURES = [[25,'aggressive'],[50,'measured'],[75,'keen'],[100,'eager']];
document.querySelectorAll('.negoform').forEach(form => {
  const slider = form.querySelector('input[name=posture]');
  const label = form.querySelector('.postval');
  const show = () => { label.textContent = (POSTURES.find(p => slider.value <= p[0]) || POSTURES[3])[1]; };
  slider.addEventListener('input', show); show();

  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const st = form.querySelector('.status');
    const out = form.querySelector('.drafts');
    const id = form.dataset.id;
    const fd = new FormData(form);
    out.innerHTML = '';
    say(st, 'asking… this runs on GitHub and takes a minute or two.');
    try {
      const before = await readJson(CFG.draftsDir + '/' + id + '.json');
      const stamp = before.data ? before.data.generated_at : null;
      await api('/repos/' + CFG.repo + '/actions/workflows/' + CFG.negotiateWorkflow + '/dispatches', {
        method: 'POST',
        body: JSON.stringify({ ref: CFG.branch, inputs: {
          listing_id: id, posture: String(fd.get('posture')),
          conversation: String(fd.get('conversation') || ''),
          notes: String(fd.get('notes') || '')
        } })
      });
      // Poll the committed file rather than the run, so a failure that still writes a
      // reason surfaces as that reason instead of a red X you have to go hunting for.
      for (let i = 0; i < 60; i++){
        await new Promise(r => setTimeout(r, 5000));
        const now = await readJson(CFG.draftsDir + '/' + id + '.json');
        if (now.data && now.data.generated_at !== stamp){ renderDrafts(out, st, now.data); return; }
        say(st, 'still working… ' + ((i + 1) * 5) + 's');
      }
      say(st, 'Gave up waiting after five minutes. Check the Actions tab — the run may '
        + 'still be going, and the drafts will appear here when it finishes.', 'bad');
    } catch (err) { say(st, err.message, 'bad'); }
  });
});

function renderDrafts(out, st, data){
  if (data.status !== 'ok'){
    say(st, data.error || 'Drafting failed and gave no reason.', 'bad'); return;
  }
  say(st, data.posture_label + ' · walk away above $'
    + Math.round((data.walkaway_price_cents || 0) / 100), 'ok');
  out.innerHTML = data.drafts.map(d => {
    const over = (d.over_walkaway_cents || []).length
      ? '<p class="reason">⚠ mentions $'
        + d.over_walkaway_cents.map(c => Math.round(c / 100)).join(', $')
        + ' — above your walk-away. Check before sending.</p>' : '';
    const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    return '<div class="draft"><p class="dtext">' + esc(d.text) + '</p>'
      + '<p class="basis">' + esc(d.rationale) + '</p>' + over
      + '<button type="button" class="copy">Copy</button></div>';
  }).join('');
  out.querySelectorAll('.copy').forEach(btn => btn.addEventListener('click', () => {
    const text = btn.parentElement.querySelector('.dtext').textContent;
    navigator.clipboard.writeText(text)
      .then(() => { btn.textContent = 'Copied'; })
      .catch(() => { btn.textContent = 'Copy failed — select the text instead'; });
  }));
}

checkConnection();
</script>
</body>
</html>
"""
