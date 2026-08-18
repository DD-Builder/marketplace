"""Provider-agnostic valuation seam.

The funnel calls one interface — ``ValuationProvider.appraise(listing, vertical)`` — and
never knows which model answered. Config picks the driver, so you can lean on your Claude
Max subscription first and flip to the metered API (or later GPT / Gemini / Grok) without
touching anything downstream.

Status of the drivers:

* **claude-api** — fully implemented; wraps the Opus vision appraisal over the metered
  Anthropic API (pay-per-token).
* **claude-code** — routes through the local Claude Code CLI so calls draw on your Max
  *subscription* instead of the API bill. Scaffolded here with the exact invocation; it
  needs an environment where ``claude`` is installed and logged in to your Max plan to
  validate (that auth can't be exercised from a bare sandbox).
* **openai / gemini / grok** — declared seams. Each raises a clear "wire me in" error until
  implemented; adding one is a single class, no funnel changes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from dealfinder.core.schemas import AppraisalResult, RawListing
from dealfinder.logging import get_logger
from dealfinder.verticals import DEFAULT_VERTICAL, Vertical

log = get_logger(__name__)


def _one_line(text: str) -> str:
    """Flatten third-party text to a single line before it goes into a prompt.

    Comp titles are written by strangers. One newline lets a title break out of its
    ``- {title}: ${price}`` row and read as a fresh instruction to the model. eBay caps
    titles at 80 characters and rejects newlines, so this isn't reachable today — but
    this block is the shared seam for every future comps source, and the guard is a
    single call.
    """
    return " ".join(str(text).split())


def comps_prompt_block(comps) -> str:
    """Render comparables for an appraisal prompt.

    Deliberately labels asking prices as such. eBay's free API exposes active listings
    only, and an asking price is what a hopeful seller wants rather than what anyone
    paid — anchoring a restored-value estimate to unsold asks inflates it. Saying so is
    what lets the model discount them instead of averaging them.
    """
    if not comps:
        return ""
    asks = [c for c in comps if not c.is_sold]
    sold = [c for c in comps if c.is_sold]
    out = []
    if sold:
        out.append("Comparable SOLD prices (what buyers actually paid):")
        out += [f"- {_one_line(c.title)}: ${c.price_cents / 100:.0f}"
                + (f" [{_one_line(c.condition)}]" if c.condition else "") for c in sold]
    if asks:
        out.append(
            "Comparable ASKING prices (currently listed, NOT sold — sellers ask more "
            "than pieces fetch, and unsold items may be overpriced; treat these as a "
            "soft ceiling, not a market value):"
        )
        out += [f"- {_one_line(c.title)}: ${c.price_cents / 100:.0f}"
                + (f" [{_one_line(c.condition)}]" if c.condition else "") for c in asks]
    out.append(
        "These come from a keyword search on the seller's own title, so some may be the "
        "wrong item entirely. Ignore any that don't match what you see in the photos."
    )
    return "\n\n" + "\n".join(out)


@runtime_checkable
class ValuationProvider(Protocol):
    name: str

    def appraise(
        self,
        listing: RawListing,
        vertical: Vertical = DEFAULT_VERTICAL,
        *,
        image_paths: list[Path] | None = None,
        comps: list | None = None,
        #: Where and how the piece is selling, when that is itself price evidence. An
        #: auction near close with many bids is the strongest comparable available for
        #: the object in front of you, and withholding it was letting the model invent a
        #: number in a vacuum.
        venue: str = "",
    ) -> AppraisalResult:
        ...


class ClaudeApiAppraiser:
    """Opus vision appraisal over the metered Anthropic API (pay-per-token)."""

    name = "claude-api"

    def appraise(self, listing, vertical=DEFAULT_VERTICAL, *, image_paths=None,
                 comps=None, venue=""):
        from dealfinder.valuation import appraise as appraise_mod

        image_urls = None if image_paths else [p.remote_url for p in listing.photos]
        result, _in, _out = appraise_mod.appraise(
            description=listing.description,
            asking_price_cents=listing.asking_price_cents,
            image_paths=image_paths,
            image_urls=image_urls,
            comps=comps,
            guidance=vertical.appraiser_guidance,
        )
        return result


# JSON contract the subscription CLI is asked to return (mirrors AppraisalResult).
_CLI_SCHEMA_HINT = (
    '{"identified_item": str, "style_era": str, "maker_guess": str|null, '
    '"materials": [str], "condition_assessment": str, "est_asis_value_cents": int, '
    '"est_restored_resale_value_cents": int, "est_restoration_cost_cents": int, '
    '"est_restoration_effort_hours": float, "confidence": 0..1, "deal_score": 0..100, '
    '"reasoning": str}'
)


#: The valuation discipline, kept in one place because it is the part that decides
#: whether a number is worth acting on. It replaced a body that opened "advising a
#: restoration reseller" and then asked about "solid wood vs veneer vs particleboard" —
#: text sent verbatim when valuing a painting. Asked to value a Nino Pippa oil that way,
#: the model reasoned from "working regional landscape artists of this caliber" and
#: returned $1,200; the artist's actual realised results run $111-$401, and the same run
#: valued a second Pippa at $450. A category judgement is not a comparable.
_VALUATION_RULES = (
    "How to value this:\n"
    "1. NAME THE MARKET, NOT THE CALIBRE. If a maker, artist, brand or workshop is "
    "identifiable, base the estimate on what THAT NAME's work actually realises at "
    "auction. Do not reason from 'pieces of this quality' or 'artists of this calibre' — "
    "that produces a plausible number attached to nothing. If you cannot recall specific "
    "realised results for this name, say so in `reasoning` and set confidence <= 0.4.\n"
    "2. REALISED, NOT ASKED. Gallery, 1stDibs, Etsy, Chairish and artist-direct prices "
    "are asks by hopeful sellers, and many never sell. Auction hammer prices and eBay "
    "SOLD listings are transactions. Value on transactions. Where the two disagree by "
    "several times — common for living decorative artists — the realised price is the "
    "market and the ask is marketing.\n"
    "3. VALUE IT AS IT ARRIVES. `est_asis_value_cents` is the figure that decides "
    "whether to buy, and it means: sold in its current condition, no restoration, no "
    "waiting for the right buyer. Fill in the restored fields too, but do not let a "
    "restoration premium leak into the as-is number.\n"
    "4. GENUINE VS STYLED-AFTER. If the piece is in the manner of a maker rather than "
    "by them, or could be a reproduction of an original, value it as the look-alike and "
    "lower confidence. State which you assumed.\n"
    "5. BE WILLING TO RETURN A SMALL NUMBER. Most lots at an estate auction are worth "
    "tens or low hundreds of dollars. An estimate that makes an ordinary lot look like a "
    "find is the expensive kind of wrong: it is what causes real money to be overbid. "
    "When torn between two figures, return the lower one."
)


class ClaudeCodeAppraiser:
    """Subscription path: drive the Claude Code CLI so calls bill to your Max plan, not the API.

    This is what makes the whole pipeline free to run. In CI (GitHub Actions) the CLI
    authenticates from ``CLAUDE_CODE_OAUTH_TOKEN`` — a long-lived token you mint once with
    ``claude setup-token`` and store as a repo secret. Locally it uses whatever login the
    CLI already has.

    Photos are read off disk by the CLI's Read tool, so download them first (they're also
    only valid for a few hours). Falls back to a text-only appraisal when no images exist,
    with the confidence penalty that deserves.
    """

    name = "claude-code"

    def __init__(self, cli: str = "claude", model: str = "", timeout: float = 300.0) -> None:
        self.cli = cli
        self.model = model
        self.timeout = timeout

    def appraise(self, listing, vertical=DEFAULT_VERTICAL, *, image_paths=None,
                 comps=None, venue=""):
        if not shutil.which(self.cli):
            raise RuntimeError(
                f"'{self.cli}' CLI not found. The subscription appraiser needs Claude Code "
                "installed and authenticated (locally, or via CLAUDE_CODE_OAUTH_TOKEN in CI). "
                "Set APPRAISER_PROVIDER=claude-api to use the metered API instead."
            )
        paths = [Path(p) for p in (image_paths or [])]
        paths = [p for p in paths if p.exists()][:6]  # bound image cost per appraisal

        price = (
            f"${listing.asking_price_cents / 100:.0f}"
            if listing.asking_price_cents is not None
            else "unknown"
        )
        if paths:
            img_block = (
                "Use the Read tool to look at EACH of these photos before judging:\n"
                + "\n".join(str(p) for p in paths)
            )
        else:
            img_block = (
                "No photos are available — appraise from the text alone and set confidence "
                "no higher than 0.35, since construction can't be verified."
            )

        prompt = (
            "You are an expert appraiser advising a reseller. "
            f"{vertical.appraiser_guidance}\n\n"
            f"{img_block}\n\n"
            f"{venue or f'Asking price: {price}'}\n"
            f"Title: {listing.title[:300]}\n"
            f"Description: {listing.description[:2000]}"
            f"{comps_prompt_block(comps or [])}\n\n"
            f"{_VALUATION_RULES}\n\n"
            "Return ONLY a JSON object, no prose, matching this shape (money in US cents):\n"
            f"{_CLI_SCHEMA_HINT}"
        )

        cmd = [self.cli, "-p", prompt, "--output-format", "json"]
        if paths:
            cmd += ["--allowedTools", "Read"]
            for d in {str(p.parent.resolve()) for p in paths}:
                cmd += ["--add-dir", d]
        if self.model:
            cmd += ["--model", self.model]

        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=self.timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed: {cli_failure_reason(proc.stdout, proc.stderr)}"
            )
        return _parse_cli_json(proc.stdout)


def cli_failure_reason(stdout: str, stderr: str = "") -> str:
    """Explain a non-zero exit from the Claude Code CLI.

    The CLI prints its envelope on stdout even when it fails, and the actual reason lives
    in ``result`` — well past the usage/session fields. Reporting the raw first 400
    characters therefore showed a wall of zeroed token counters and truncated the one
    sentence that mattered, which is exactly what happened on the first real Action run.
    """
    try:
        env = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        env = None
    if isinstance(env, dict):
        detail = str(env.get("result") or env.get("error") or "").strip()
        if detail:
            low = detail.lower()
            if any(w in low for w in ("auth", "credential", "token", "login", "unauthor")):
                detail += (
                    " — this reads like a credential problem. Run `claude setup-token` "
                    "again and replace the CLAUDE_CODE_OAUTH_TOKEN repository secret."
                )
            return detail[:400]
        # No `result` at all: report the fields that actually distinguish the failure.
        usage = env.get("usage") or {}
        return (
            f"the CLI exited with is_error={env.get('is_error')}, "
            f"stop_reason={env.get('stop_reason')!r}, "
            f"turns={env.get('num_turns')}, "
            f"input_tokens={usage.get('input_tokens')}, "
            f"cost=${env.get('total_cost_usd')} — zero tokens and zero cost mean the "
            "request never reached the API, which is almost always authentication."
        )
    return (stderr or stdout or "no output at all")[:400]


def extract_cli_json(stdout: str) -> str:
    """Pull the JSON object out of a Claude Code CLI reply.

    The CLI wraps the answer in an envelope, and the answer itself may be fenced in
    markdown. Shared with the negotiation drafter, which talks to the same CLI.
    """
    text = stdout
    try:
        env = json.loads(stdout)
        if isinstance(env, dict):
            if env.get("is_error"):
                # The envelope's leading fields are noise; `result` carries the real
                # reason (bad credentials, rate limit, refusal). Surface that.
                detail = env.get("result") or env.get("error") or "no detail given"
                raise RuntimeError(f"claude CLI error: {str(detail)[:300]}")
            text = env.get("result") or env.get("text") or stdout
    except json.JSONDecodeError:
        pass
    if "```" in text:  # strip a ```json fence if the model added one
        parts = text.split("```")
        for part in parts:
            cleaned = part[4:] if part.startswith("json") else part
            if "{" in cleaned:
                text = cleaned
                break
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"no JSON object in claude CLI output: {text[:200]!r}")
    return text[start : end + 1]


def _parse_cli_json(stdout: str) -> AppraisalResult:
    return AppraisalResult.model_validate_json(extract_cli_json(stdout))


class _UnimplementedProvider:
    """A declared seam for a model we haven't wired yet."""

    def __init__(self, name: str) -> None:
        self.name = name

    def appraise(self, listing, vertical=DEFAULT_VERTICAL, *, image_paths=None,
                 comps=None, venue=""):
        raise NotImplementedError(
            f"The '{self.name}' appraiser is a declared seam, not yet implemented. "
            "It plugs in as one ValuationProvider class with no funnel changes."
        )


#: The model the valuation call runs on when nothing pins it. Sonnet is the right
#: default here rather than the CLI's own: valuation is the one metered step in the
#: pipeline and it runs on a schedule, so the model has to be one you're happy burning
#: subscription capacity on hourly. Override per-repo with the APPRAISE_MODEL variable
#: (``claude-opus-5`` when you want the most careful read on a hard category, or an
#: empty string to fall back to whatever the CLI is configured for).
DEFAULT_APPRAISE_MODEL = "claude-sonnet-5"


def _appraise_model() -> str:
    """The model to pin the valuation call to. Explicit empty means 'CLI default'."""
    raw = os.getenv("APPRAISE_MODEL")
    if raw is None:
        return DEFAULT_APPRAISE_MODEL
    raw = raw.strip()
    # An unset repository variable arrives as "" in Actions, which must not be read as a
    # deliberate "use the CLI default" — that's the same empty-string trap _env() exists
    # for on the run_board side.
    return raw or DEFAULT_APPRAISE_MODEL


_BUILDERS = {
    "claude-api": lambda: ClaudeApiAppraiser(),
    "claude-code": lambda: ClaudeCodeAppraiser(model=_appraise_model()),
    "openai": lambda: _UnimplementedProvider("openai"),
    "gemini": lambda: _UnimplementedProvider("gemini"),
    "grok": lambda: _UnimplementedProvider("grok"),
}


def get_appraiser(provider: str = "claude-code") -> ValuationProvider:
    """Build the configured appraiser. Defaults to the subscription (Max) path."""
    key = (provider or "claude-code").lower().strip()
    builder = _BUILDERS.get(key)
    if builder is None:
        raise ValueError(f"unknown appraiser provider {provider!r}; known: {sorted(_BUILDERS)}")
    return builder()


def available_providers() -> list[str]:
    return sorted(_BUILDERS)
