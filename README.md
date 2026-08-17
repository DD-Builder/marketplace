# The Bench

Finds underpriced furniture on Facebook Marketplace, values it with AI, and helps you price
and negotiate the resale — from an iPad, for as close to free as this can be built.

It runs entirely on GitHub: **Actions** for compute, **Pages** for hosting, and your
**Claude subscription** for the valuations, driven through the Claude Code CLI so there's no
metered API bill. The only thing that costs money is the scrape itself.

## ⚠️ Read this first

- **There is no official Facebook Marketplace API.** Listings are obtained through a
  third-party scraping service (Apify), which is a breach of Meta's Terms of Service even
  though the data is public. Running this is your decision and your risk. Nothing here logs
  into Facebook or touches your account — but the underlying activity is still against
  Meta's terms.
- **Valuations are estimates from photos and text.** Verify condition in person before
  handing over money.
- **Nothing is ever sent to a seller.** The app drafts messages; you read, edit, and send
  them yourself.

---

## How it works

```
search grid (cheap)  ->  detail pages for new/cheaper listings only
                     ->  pre-screen (free, keyword + photo signal)
                     ->  AI appraisal of the survivors, capped per run
                     ->  deterministic scoring, authenticity check, resale pricing
                     ->  a ranked static page committed to docs/
```

Three ideas carry the whole design:

**Never pay twice for the same listing.** Apify bills when it fetches, so the run does a
cheap index scan first and only opens the detail pages of listings that are new or newly
cheaper. Measured on a real 91-listing export, a daily re-run finds ~30% new and ~70%
already seen — which is why *daily* is the cheap cadence, not the expensive one.

**Value the object once, score it every run.** An appraisal answers "what is this and what
is it worth restored", which doesn't change when the seller cuts the price. So appraisals
are stored in `docs/catalog.json` and the *scores* are recomputed each run. A price drop
re-ranks for nothing, and improving the ranking logic retroactively improves every piece
already found.

**Price to the market, not to your costs.** The headline sell target is what a restored
piece fetches regionally, independent of you. Your hours and materials produce a *second*
number — profit and effective hourly wage — which you can look at or ignore. Folding your
weekend into the ask is how a $450 table gets listed at $978 and never sells.

## The board

`docs/index.html`, served by Pages. Ranked cards with photo, asking price, estimated
restored value, margin, and flags (★ killer deal, ▼ price drop, ⚠ look-alike, ⤢ out of
radius). Look-alike detection reads the listing for tells like "Eames-*esque*" or
"Barcelona *style*" and marks the piece accordingly.

Paste a fine-grained GitHub token into **Connection** once, and the page can also write:

- **Scrape now** — runs the pipeline on demand.
- **Log this piece** — price paid, materials, hours, sold-for; writes `docs/pieces.json`,
  which feeds your personal resale numbers and your realised profit history.
- **Draft a message to the seller** — a posture slider from *ready to walk* to *pay asking
  today*, plus the thread so far. Drafts are generated on Actions and appear on the card.

The token lives in your browser's localStorage. Be clear-eyed about it: Contents-write
scope can push code (including workflow changes that read repo secrets), and localStorage
is shared across every GitHub Pages project site on your username — so scope the token to
this one repository and treat it as disposable. Drafts you generate (including pasted
seller messages) are committed to `.drafts/`, outside the published site — though on a
public repo the repository itself is still readable.

## The Gavel — EBTH auction watch

`docs/auctions/index.html`, a second board for [Everything But The House](https://www.ebth.com)
lots, where the problem inverts: nobody names a price, the price *finds itself* — bids sit
low for days, then most of the money arrives in the closing hours. So the tracker runs
**hourly** (`auctions.yml`), asymmetrically: bid snapshots every run, discovery of new lots
every ~6h, one appraisal per lot ever.

For each quality lot (the same vertical keyword gate as the Marketplace side, but requiring
a positive signal) it holds the full bid history and answers three questions:

- **Your max bid** — worked backwards from the appraisal: restored value, minus restoration,
  your hours, your margin, freight, and the buyer's premium riding the hammer. Decided
  before the endgame, so the endgame can't decide it for you.
- **Projected close** — the *endgame multiplier* says what T-24h prices become by the
  hammer. It starts as a prior (2×) and is **learned from this catalogue's own ended lots**:
  every watched auction that closes contributes a `(T-24h bid, final price)` pair, so the
  projection sharpens with every week the tracker runs.
- **Stance** — `BID LATE` (final day, headroom), `WATCH` (early — never bid early, it only
  feeds the price), `OUTPRICED` (bidding or its projection passed your ceiling), `PASS`.

The dev sandbox can't reach ebth.com, so the parsers are layered (JSON-LD → any embedded
JSON state → HTML) and self-reporting: run the workflow with **probe** checked and it
commits `docs/auctions/probe.json` describing what the site actually serves, so extraction
is tightened from evidence, not guesswork.

## Setup

1. **Fork or clone**, then enable Pages: *Settings → Pages → Deploy from branch → `docs/`*.
2. **Secrets** (*Settings → Secrets and variables → Actions → Secrets*):
   - `CLAUDE_CODE_OAUTH_TOKEN` — from `claude setup-token`. This is what makes the AI free.
   - `APIFY_TOKEN` — from apify.com. The free tier is $5/month of credit.
3. **Variables** (same page, *Variables* tab) — all optional:

   | Variable | Default | What it does |
   |---|---|---|
   | `SEARCH_URLS` | one dresser search | Marketplace search URLs, one per line. Each is a separate bill. |
   | `RESULTS_LIMIT` | 60 | Listings per search. The main scrape-cost lever. |
   | `MAX_APPRAISALS` | 12 | Hard cap on AI calls per run. |
   | `MIN_PRICE_DOLLARS` / `MAX_PRICE_DOLLARS` | unset | Pushed into the search URL, so out-of-range listings are never billed. |
   | `DAYS_SINCE_LISTED` | unset | Same, for recency. |
   | `IN_RADIUS_TOWNS` | Lexington + 40mi | Comma-separated town names used to flag distance. |
   | `HOURLY_RATE_CENTS` | 3000 | What your bench time is worth. |
   | `MAX_CARDS_PER_TIER` | 8 | Cards shown per price band (Estate / Mid / Quick). |
   | `PHOTO_RETENTION_DAYS` | 30 | Photos are deleted after this; entries and price history stay. |

   Two optional *secrets* unlock market comparables: `EBAY_CLIENT_ID` and
   `EBAY_CLIENT_SECRET` (free Browse API, ~5k calls/day). Without them the appraiser
   estimates unaided, exactly as before. See `docs/SETUP.md` step 3b.

4. **Run it**: *Actions → Deal board → Run workflow*. It also runs daily at 13:17 UTC.

## Costs

| | |
|---|---|
| GitHub Actions + Pages | free on public repos |
| Valuations | your existing Claude subscription — no API bill |
| Scraping | the only real cost; Apify's free tier is $5/month |

The defaults (one search, 60 results, 12 appraisals) are sized to sit inside the free tier.
Earlier defaults burned a month's credit in two runs, which is why they're deliberately
small now — widen them once you know what a run costs you.

## Running it locally

```bash
pip install -e '.[dev]'
python -m pytest                                   # no network, no spend

# A full offline run over a saved Apify export (any dataset JSON you've downloaded):
python -m dealfinder.run_board --from-json my_export.json --out /tmp/site --dry-run

# Verify the two-stage scrape against Apify for a few cents:
python -m dealfinder.sources.scrape
```

## Layout

| Path | |
|---|---|
| `run_board.py` | the whole run: scrape → appraise → rank → publish |
| `sources/scrape.py` | two-stage scrape + the fallback ladder |
| `sources/apify.py` | Apify REST client and record adapter |
| `catalog.py` | persistent listings + stored appraisals |
| `selection.py` | cost control: dedup, seen-diff, cap |
| `prescreen.py` / `verticals.py` | the free junk filter and its per-niche knowledge |
| `engine.py` | `evaluate_piece` — scoring a listing against an appraisal |
| `ranking.py` | priority, liquidity, heat, badges (`BADGE_DEFS` feeds the page legend) |
| `appraiser.py` | provider seam: subscription CLI, metered API, or a future model |
| `authenticity.py` | look-alike and knockoff detection |
| `resale.py` | market price and your-numbers pricing |
| `restoration.py` | bounds on the model's cost/effort estimate, from published survey data |
| `sources/ebay.py` | free Browse API comps — market anchors for the appraisal |
| `sources/ebth.py` | EBTH auction source: layered parsing + the CI structure probe |
| `auctions/` | auction catalogue, max-bid math, endgame calibration, the Gavel page |
| `run_auctions.py` | the hourly auction run: snapshot → appraise → advise → publish |
| `pieces.py` | your books: costs, sales, realised hourly wage |
| `negotiation/` | posture, prompt building, drafters |
| `board.py` + `templates/` | the static page: data → markup in Python, skeleton/CSS/JS as real files |

## Honest limits

- **Item-detail fetching through the Apify actor is unverified.** It's isolated behind one
  function with a three-rung fallback (two-stage → single-stage → thin records), and the
  verdict is remembered, so finding out costs at most one run.
- **Search-URL filters are Facebook's own parameters**, passed through verbatim. If
  Facebook renames one it's silently ignored — a lost saving, not a broken run.
- **1stDibs and similar dealer listings are treated as heavily-discounted ceilings**, not
  comparables, because they systematically overprice.
