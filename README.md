# Deal Finder

Self-hosted app that finds underpriced furniture on Facebook Marketplace, values each
piece with Claude (vision), scores it as a restoration-flip deal, and helps you draft
negotiation messages. Runs entirely on your own machine.

## ⚠️ Read this first — limitations & risk

- **There is no official Facebook Marketplace API.** Everything that ingests listings is
  unofficial and **violates Meta's Terms of Service.** Courts generally treat scraping
  *public* data as non-criminal, but it remains a ToS breach; Meta can ban accounts/IPs.
  Running this is your decision and your risk.
- The scraper is built to be low-footprint (logged-out first, a single warmed *burner*
  account as fallback, residential IP, human-like pacing, a strict rate governor). It is
  **not** ToS-compliant and can still get an account disabled. Keep your personal account
  out of it entirely, and expect periodic maintenance when Facebook changes its markup.
- **Negotiation is human-in-the-loop.** You paste the seller's message; the app drafts a
  reply tuned to a posture slider (aggressive ↔ eager). You review and send it yourself.
  Nothing is auto-sent.

## Architecture

Two processes share one SQLite DB:

- `dealfinder-worker` — scrapes, triages, appraises, scores, persists (APScheduler loop).
- `dealfinder-web` — the dashboard (FastAPI + Jinja2, `http://127.0.0.1:8000`).

Valuation is two-tier: cheap **Haiku** text triage filters listings, then **Opus 4.8**
vision appraises the promising ones and returns structured estimates. The deal score is
computed deterministically from those numbers (`restored_resale − asking − restoration
cost − effort×your_hourly_rate`, scaled and weighted by confidence).

## Setup

```bash
pip install -e .              # or: pip install -e ".[dev]" for tests
patchright install chrome     # once, to install the browser patchright drives
cp .env.example .env          # then set ANTHROPIC_API_KEY
```

Key `.env` values: `ANTHROPIC_API_KEY` (required), `HOURLY_RATE_CENTS` (calibrate the
deal score to your own labour value), `RATE_MAX_ACTIONS_PER_HOUR` (the main ban lever),
`HEADLESS=false` (headed Chrome is stealthier).

### Optional: burner session for gated content

Logged-out scraping covers a lot but Facebook gates some pages behind login. To use a
**dedicated, non-personal** burner account as fallback, log in once manually and save the
Playwright `storage_state`, then point `FB_SESSION_PATH` at it. No raw password is ever
stored. Warm the account with normal activity before relying on it; if it hits a
checkpoint the app cools it down and never tries to solve challenges.

## Run

```bash
# 1. Start the dashboard and add a search target at /targets
dealfinder-web

# 2. Trigger a scrape — either "Scrape now" on a target, or:
dealfinder-worker --once            # run every enabled target once
dealfinder-worker --once --target 1 # just one target

# 3. Or run the always-on scheduler
dealfinder-worker
```

The feed at `/` lists scored deals; click one for the full AI valuation and the
negotiation panel. `/status` shows scrape-run history and worker health.

## Development

```bash
pip install -e ".[dev]"
pytest
```

`tests/test_parse.py` runs against saved Facebook-markup fixtures — the first thing to
fail (and fix) when Facebook changes its embedded JSON shape.

## Roadmap (built in phases)

Phase 1 (this) is the thin end-to-end slice. Later phases deepen anti-detection, add full
price-band enumeration at scale, alerting on high-score deals, and real pricing
comparables (eBay sold-listings / your own price history) via the `comparables.py` seam.
