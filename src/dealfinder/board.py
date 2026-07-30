"""Render the deal board — the static page GitHub Pages serves to your iPad.

Takes the engine's ranked output and writes a self-contained HTML file (plus local photo
files) into an output directory. No server, no database: the whole site is regenerated each
run and committed, which is what makes hosting free.

The page's skeleton, stylesheet and script live as real files in ``templates/`` (so they
get editor tooling and diffs) and are inlined at render time — the shipped artifact stays
one HTML file that renders with zero network requests.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from dataclasses import dataclass
from importlib.resources import files as _pkg_files
from pathlib import Path

from dealfinder.engine import EvaluatedPiece, RunResult
from dealfinder.ranking import BADGE_DEFS, TIERS
from dealfinder.resale import Posture

_POSTURE_LABEL = {
    Posture.MARKET: "market",
    Posture.KNOWN_PREMIUM: "known · premium",
    Posture.CEILING_TEST: "ceiling test",
}


def _template() -> str:
    tpl = _pkg_files("dealfinder").joinpath("templates")
    page = tpl.joinpath("board.html").read_text(encoding="utf-8")
    page = page.replace("/*{{CSS}}*/", tpl.joinpath("board.css").read_text(encoding="utf-8"))
    return page.replace("/*{{JS}}*/", tpl.joinpath("board.js").read_text(encoding="utf-8"))


def _money(cents: int | None, *, none: str = "—") -> str:
    """Dollars for humans. None is *unknown*, not free — the two used to render alike,
    and a parse failure was indistinguishable from a $0 curb find."""
    if cents is None:
        return none
    sign = "−" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.0f}"


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
    head = re.split(r"[:;(—]|,\s", identified_item.strip(), maxsplit=1)[0].strip()
    head = head or identified_item.strip() or "no photo"
    if len(head) > limit:
        head = head[:limit].rsplit(" ", 1)[0] + "…"
    return head.title()


def _clip(text: str, limit: int) -> str:
    """Word-boundary truncation with a visible ellipsis — a sentence chopped mid-word
    ('collapsed demand for large con') reads as a rendering bug."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


@dataclass
class BoardMeta:
    title: str = "The Bench"
    region: str = "Lexington · 40 mi"
    generated_at: str = ""
    generated_at_iso: str = ""
    note: str = ""
    # Where the page sends its writes. Empty ``repo`` renders a read-only board — every
    # button explains that it isn't wired up rather than failing silently.
    repo: str = ""                              # "owner/repo"
    branch: str = "main"
    board_workflow: str = "deal-board.yml"
    negotiate_workflow: str = "negotiate.yml"
    # Repo-relative paths consumed by the GitHub Contents API — deliberately NOT URLs,
    # and not filesystem paths either. "docs/pieces.json" is exactly right in Actions.
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
            <label>Paid <input name="paid" inputmode="decimal"
              placeholder="{ask / 100:.0f}"></label>
            <label>Materials <input name="materials" inputmode="decimal"
              placeholder="0"></label>
            <label>Hours <input name="hours" inputmode="decimal"
              placeholder="0"></label>
            <label>Sold for <input name="sold" inputmode="decimal"
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


def _restoration_notes(p: EvaluatedPiece) -> str:
    """Say so when the model's restoration estimate was clamped to published reality.

    Silently rewriting a number the appraiser produced would make the card a lie about
    its own reasoning — and these corrections move the margin, so they're worth reading.
    """
    if not p.restoration_notes:
        return ""
    items = "".join(f"<li>{html.escape(n)}</li>" for n in p.restoration_notes)
    return (
        '<p class="basis">Restoration estimate adjusted against published refinishing '
        f'costs:</p><ul class="clamped">{items}</ul>'
    )


def _card(rank: int, p: EvaluatedPiece, photos: list[str]) -> str:
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
    label = _short_label(p.appraisal.identified_item)
    alt = html.escape(p.appraisal.identified_item[:120])
    was_cents = listing.raw_json.get("_was_price_cents")

    cover = photos[0] if photos else None
    if cover:
        thumb = (
            f'<img class="thumb" src="{html.escape(cover)}" alt="{alt}" '
            f'data-ph="{html.escape(label)}" loading="lazy">'
        )
    else:
        # The span carries the only textual identity of the piece, so it must NOT be
        # aria-hidden — a VoiceOver user would get nothing at all where the photo goes.
        thumb = f'<div class="thumb ph"><span>{html.escape(label)}</span></div>'
    hint = (
        f'<button type="button" class="gallery-hint">{len(photos)} photos</button>'
        if len(photos) > 1 else ""
    )

    # The swing tag: ask price, strike-through if dropped, margin beneath.
    neg = p.cash_margin_cents < 0
    was = f'<span class="tag-was">{_money(was_cents)}</span>' if was_cents else ""
    tag = (
        f'<div class="swingtag{" neg" if neg else ""}">'
        f'<span class="tag-price">{_money(listing.asking_price_cents)}</span>{was}'
        f'<span class="tag-margin">{_money(p.cash_margin_cents)} margin</span></div>'
    )
    star = '<div class="starsticker">★ Killer</div>' if p.is_killer else ""

    klass = (
        "card killer"
        if p.is_killer
        else ("card flagged" if p.authenticity.is_red_flag else "card")
    )
    # Everything the page's JS needs lives in data-* attributes; it must never scrape
    # badge text or heading text out of the markup again.
    data = (
        f' data-id="{html.escape(listing.fb_listing_id)}"'
        f' data-title="{html.escape(listing.title or "Untitled")}"'
        f' data-priority="{p.priority}"'
        f' data-margin="{p.cash_margin_cents}"'
        f' data-ask="{listing.asking_price_cents if listing.asking_price_cents is not None else ""}"'
        f' data-fresh="{p.days_since_seen:.2f}"'
        f' data-killer="{1 if p.is_killer else 0}"'
        f' data-flag="{1 if p.authenticity.is_red_flag else 0}"'
        f' data-oor="{1 if p.out_of_radius else 0}"'
        f' data-tier="{html.escape(p.tier)}"'
        f" data-photos='{html.escape(json.dumps(photos))}'"
    )
    # An empty href reloads the page (losing half-typed notes); a non-https listing URL
    # from a third-party actor is not something we hand the browser.
    view = (
        f'<a class="view" href="{html.escape(listing.url)}" target="_blank" '
        'rel="noopener">View listing →</a>'
        if listing.url.startswith("https://")
        else ""
    )
    net_class = "fig net neg" if neg else "fig net"
    return f"""
    <article class="{klass}"{data}>
      <div class="hero">
        <div class="rank">{rank}</div>
        {thumb}{hint}{tag}{star}
      </div>
      <div class="body">
        <div class="head">
          <h3>{html.escape(listing.title) or "Untitled"}</h3>
          <div class="loc">{html.escape(listing.location_text)}</div>
        </div>
        <div class="chips">{chips}</div>
        {warn}
        <div class="figs">
          <div class="fig"><span class="k">Ask</span><span class="v">{_money(listing.asking_price_cents)}</span></div>
          <div class="fig"><span class="k">Est. resale</span><span class="v">{_money(p.appraisal.est_restored_resale_value_cents)}</span></div>
          <div class="{net_class}"><span class="k">Est. margin</span><span class="v">{_money(p.cash_margin_cents)}</span></div>
          <div class="fig"><span class="k">Last confirmed</span><span class="v {_seen_tone(p.days_since_seen)}">{_seen_label(p.days_since_seen)}</span></div>
        </div>
        {_resale_row(p)}
        <div class="meters">
          <div class="meter"><label>Priority</label><div class="bar"><i style="width:{p.priority}%"></i></div><b>{p.priority:.0f}</b></div>
          <div class="meter"><label>Sells</label><div class="bar sub"><i style="width:{p.liquidity}%"></i></div><b>{p.liquidity:.0f}</b></div>
          <div class="meter"><label>Heat</label><div class="bar sub"><i style="width:{p.heat}%"></i></div><b>{p.heat:.0f}</b></div>
        </div>
        <details class="why"><summary>Why this valuation</summary><p>{html.escape(_clip(p.appraisal.reasoning, 600))}</p>{_restoration_notes(p)}</details>
{_tools(p)}
        {view}
      </div>
    </article>"""


def _legend() -> str:
    return "\n".join(
        f'  <span class="chip {tone}"><span class="ico">{html.escape(icon)}</span>'
        f"{html.escape(label)}</span>"
        for icon, label, tone in BADGE_DEFS.values()
    )


def _funnel_human(result: RunResult) -> str:
    plan = result.plan
    looked = plan.total_scraped
    valued = len(plan.to_appraise)
    stars = sum(1 for p in result.pieces if p.is_killer)
    bits = []
    if looked:
        bits.append(f"Looked at {looked} listings")
    if valued:
        bits.append(f"valued {valued} this run")
    bits.append(
        f"{stars} worth a hard look" if stars else "no stars today — patience pays"
    )
    return (", ".join(bits) + ".").capitalize()


def render_board(
    result: RunResult,
    *,
    meta: BoardMeta | None = None,
    photo_map: dict[str, str] | None = None,
    gallery_map: dict[str, list[str]] | None = None,
) -> str:
    """Return the complete HTML page for a run.

    ``photo_map`` maps listing id -> cover photo path; ``gallery_map`` adds the extra
    shots per id for the lightbox.
    """
    meta = meta or BoardMeta()
    photo_map = photo_map or {}
    gallery_map = gallery_map or {}
    pieces = result.pieces

    def photos_for(pid: str) -> list[str]:
        cover = photo_map.get(pid)
        extras = [g for g in gallery_map.get(pid, []) if g != cover]
        return ([cover] if cover else []) + extras

    # One grid per tier. Ranking is within a band, because comparing a $50 nightstand to
    # a $1,500 credenza on the same scale flatters whichever has the better percentage
    # and buries whichever is actually worth the drive.
    sections = []
    rank = 0
    for tier_key, label, blurb in TIERS:
        in_tier = [p for p in pieces if p.tier == tier_key]
        if not in_tier:
            continue
        cards_html = []
        for p in in_tier:
            rank += 1
            cards_html.append(_card(rank, p, photos_for(p.listing.fb_listing_id)))
        sections.append(
            f'<section class="tier" data-tier="{html.escape(tier_key)}">\n'
            f'  <div class="tierhead"><h2>{html.escape(label)}</h2>'
            f'<span class="tiernote">{html.escape(blurb)}</span>'
            f'<span class="tiercount">{len(in_tier)}</span></div>\n'
            f'  <div class="grid" id="grid-{html.escape(tier_key)}">\n'
            + "\n".join(cards_html)
            + "\n  </div>\n</section>"
        )
    cards = "\n".join(sections)
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

    # Substitution order is a security boundary: every scalar placeholder is resolved
    # against the pristine template FIRST, and the cards — which carry scraped,
    # attacker-authored text — are substituted LAST, exactly once. The old order ran
    # the remaining replacements over the card text, so a listing titled "{{CONFIG}}"
    # got the page's config JSON injected into its own heading.
    page = _template()
    for key, val in stats.items():
        page = page.replace("{{" + key + "}}", str(val))
    page = (
        page.replace("{{CONFIG}}", config)
        .replace("{{TITLE}}", html.escape(meta.title))
        .replace("{{REGION}}", html.escape(meta.region))
        .replace("{{GENERATED}}", html.escape(meta.generated_at))
        .replace("{{GENERATED_ISO}}", html.escape(meta.generated_at_iso))
        .replace("{{LEGEND}}", _legend())
        .replace("{{FUNNEL_HUMAN}}", html.escape(_funnel_human(result)))
        .replace("{{FUNNEL}}", html.escape(plan.summary()))
        .replace("{{NOTE}}", html.escape(meta.note))
    )
    return page.replace("{{CARDS}}", cards)


def write_site(
    result: RunResult,
    out_dir: Path,
    *,
    meta: BoardMeta | None = None,
    photo_files: dict[str, Path] | None = None,
    extra_photo_map: dict[str, str] | None = None,
    gallery_map: dict[str, list[str]] | None = None,
) -> Path:
    """Write index.html (and copy photos) into ``out_dir``; return the page path.

    ``extra_photo_map`` carries already-committed photos for pieces appraised on earlier
    runs, so they render without being copied again. ``gallery_map`` lists the extra
    committed shots per id for the lightbox.
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
    page.write_text(
        render_board(result, meta=meta, photo_map=photo_map, gallery_map=gallery_map),
        encoding="utf-8",
    )
    return page
