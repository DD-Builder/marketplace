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
import shutil
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from dealfinder.core.schemas import AppraisalResult, RawListing
from dealfinder.logging import get_logger
from dealfinder.verticals import DEFAULT_VERTICAL, Vertical

log = get_logger(__name__)


@runtime_checkable
class ValuationProvider(Protocol):
    name: str

    def appraise(
        self,
        listing: RawListing,
        vertical: Vertical = DEFAULT_VERTICAL,
        *,
        image_paths: list[Path] | None = None,
    ) -> AppraisalResult:
        ...


class ClaudeApiAppraiser:
    """Opus vision appraisal over the metered Anthropic API (pay-per-token)."""

    name = "claude-api"

    def appraise(self, listing, vertical=DEFAULT_VERTICAL, *, image_paths=None):
        from dealfinder.valuation import appraise as appraise_mod

        image_urls = None if image_paths else [p.remote_url for p in listing.photos]
        result, _in, _out = appraise_mod.appraise(
            description=listing.description,
            asking_price_cents=listing.asking_price_cents,
            image_paths=image_paths,
            image_urls=image_urls,
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

    def appraise(self, listing, vertical=DEFAULT_VERTICAL, *, image_paths=None):
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
            "You are an expert appraiser advising a restoration reseller. "
            f"{vertical.appraiser_guidance}\n\n"
            f"{img_block}\n\n"
            f"Asking price: {price}\n"
            f"Title: {listing.title[:300]}\n"
            f"Description: {listing.description[:2000]}\n\n"
            "Judge construction (solid wood vs veneer vs particleboard), joinery, maker marks, "
            "era, condition, and what restoration it truly needs. Value the RESTORED piece at "
            "realistic regional resale (local marketplace / eBay sold), NOT aspirational dealer "
            "listings like 1stDibs — treat those as heavily-discounted ceilings. If the piece is "
            "styled-after rather than genuine, value it as a look-alike and lower confidence.\n\n"
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
            raise RuntimeError(f"claude CLI failed: {(proc.stderr or proc.stdout)[:400]}")
        return _parse_cli_json(proc.stdout)


def _parse_cli_json(stdout: str) -> AppraisalResult:
    """Extract the appraisal JSON from the CLI output (it wraps the reply in an envelope,
    and the reply itself may be fenced in markdown)."""
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
    return AppraisalResult.model_validate_json(text[start : end + 1])


class _UnimplementedProvider:
    """A declared seam for a model we haven't wired yet."""

    def __init__(self, name: str) -> None:
        self.name = name

    def appraise(self, listing, vertical=DEFAULT_VERTICAL, *, image_paths=None):
        raise NotImplementedError(
            f"The '{self.name}' appraiser is a declared seam, not yet implemented. "
            "It plugs in as one ValuationProvider class with no funnel changes."
        )


_BUILDERS = {
    "claude-api": lambda: ClaudeApiAppraiser(),
    "claude-code": lambda: ClaudeCodeAppraiser(),
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
