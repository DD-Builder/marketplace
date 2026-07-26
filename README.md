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

The token lives only in your browser's local storage. Anyone with your unlocked device
could press these buttons; it's revocable in one click.

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

4. **Run it**: *Actions → Deal board → Run workflow*. It also runs daily at 13:00 UTC.

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

# A full offline run over a saved export — no scrape, no AI:
python -m dealfinder.run_board --from-json pilot/real_listings.json --out /tmp/site --dry-run

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
| `engine.py` | `evaluate_piece` — scoring a listing against an appraisal |
| `appraiser.py` | provider seam: subscription CLI, metered API, or a future model |
| `authenticity.py` | look-alike and knockoff detection |
| `resale.py` | market price and your-numbers pricing |
| `pieces.py` | your books: costs, sales, realised hourly wage |
| `negotiation/` | posture, prompt building, drafters |
| `board.py` | the static page, including its write side |

## Honest limits

- **Item-detail fetching through the Apify actor is unverified.** It's isolated behind one
  function with a three-rung fallback (two-stage → single-stage → thin records), and the
  verdict is remembered, so finding out costs at most one run.
- **Search-URL filters are Facebook's own parameters**, passed through verbatim. If
  Facebook renames one it's silently ignored — a lost saving, not a broken run.
- **1stDibs and similar dealer listings are treated as heavily-discounted ceilings**, not
  comparables, because they systematically overprice.
