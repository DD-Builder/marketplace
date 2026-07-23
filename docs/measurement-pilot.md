# Measurement pilot — is there real alpha in *your* market?

Before building the whole system, spend ~$15–30 to replace assumptions with data. This
pilot runs the real funnel over real listings and tells you **how many genuine deals per
1,000 you'd actually find** — the one number that decides whether any of this is worth it.
It doubles as validation that the valuation engine works on real Facebook data.

## What you need
- An [Apify](https://apify.com) account (free tier + a few dollars of credit).
- An `ANTHROPIC_API_KEY` in your environment (the pilot makes real AI calls unless `--dry-run`).
- This repo installed: `pip install -e .`

## Step 1 — Scrape a real sample (no code)
1. On Apify, open a Facebook Marketplace actor (e.g. the Cheerio-based one, ~$1.50/1k).
2. Configure your **actual targets**: your metro, your categories (dressers, sideboards,
   dining sets…), and a price ceiling. Aim for **2,000–4,000 listings** total.
3. Run it, then **Export → JSON**. Save as `listings.json`.

Cost check: 3,000 listings ≈ **$4.50** in scraping.

## Step 2 — Run the funnel
```bash
# Free first pass — see how many survive the heuristic pre-screen, no AI spend:
python pilot/run_pilot.py listings.json --dry-run

# Full funnel — triage + Opus vision appraisal (caps AI spend with --appraise-limit):
python pilot/run_pilot.py listings.json --appraise-limit 50
```
It prints the conversion at every stage and, crucially:
```
=== YOUR MARKET'S NUMBERS (per 1,000 scraped) ===
  AI-flagged deals     :   X.X   <-- feed this into the calculator
```
Appraising 50 items costs roughly **$1–2**.

## Step 3 — Verify by hand (this is the real experiment)
Open `pilot/pilot_results.csv`, sort by `deal_score`, and for each flagged deal, glance at
the listing (`url`) and fill the **`your_verdict(real?)`** column: would you actually buy
and flip it? Be honest.

Now you have the two numbers that matter:
- **True base rate** = (deals you marked real) ÷ (listings scraped) × 1,000
- **AI precision** = (deals you marked real) ÷ (deals the AI flagged)

## Step 4 — Decide
Plug your true base rate into the [economics calculator](https://claude.ai/code/artifact/bbfd4324-6e12-447a-b588-e851cb502b0f):
- **≳ 0.3–0.5% and AI precision is decent** → real signal exists; proceed with the build.
- **Near zero, or precision is poor** → the deals aren't there or the model can't spot
  them. You just saved yourself from building the whole thing for the price of lunch.

## What this also tells us
- Whether the **valuation engine** is trustworthy on real photos+descriptions (Step 3
  is a direct read on that).
- Whether the best deals are **mistitled** pieces (kept by pre-screen with "no strong
  signal" but flagged by vision) — if so, that's your edge over keyword-based competitors.
- Your real **pre-screen keep rate** and **triage rate**, which set the true AI cost.
