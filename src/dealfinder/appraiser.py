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
    """Subscription path: shell out to the Claude Code CLI so calls bill to Max, not the API.

    Requires local image files (Claude Code reads them from disk) and a ``claude`` CLI that
    is installed and authenticated to the user's subscription. Scaffolded — validate in an
    environment where that CLI is present.
    """

    name = "claude-code"

    def __init__(self, cli: str = "claude") -> None:
        self.cli = cli

    def appraise(self, listing, vertical=DEFAULT_VERTICAL, *, image_paths=None):
        if not shutil.which(self.cli):
            raise RuntimeError(
                f"'{self.cli}' CLI not found. The subscription appraiser needs Claude Code "
                "installed and logged in to your Max plan. Use provider 'claude-api' to run "
                "on the metered API instead."
            )
        if not image_paths:
            raise RuntimeError(
                "claude-code appraiser needs local image files (it reads them from disk). "
                "Run the photo-download step before appraising, or use 'claude-api' with URLs."
            )
        imgs = "\n".join(str(p) for p in image_paths)
        price = (
            f"${listing.asking_price_cents / 100:.0f}"
            if listing.asking_price_cents is not None
            else "unknown"
        )
        prompt = (
            f"{vertical.appraiser_guidance}\n\n"
            "You are an expert appraiser advising a restoration reseller. Read the image "
            f"files below plus the listing, and return ONLY a JSON object matching:\n"
            f"{_CLI_SCHEMA_HINT}\n\n"
            "Value the restored piece at realistic regional resale, not aspirational dealer "
            "prices. All money in US cents.\n\n"
            f"Asking price: {price}\nDescription: {listing.description[:2000]}\n\nImages:\n{imgs}"
        )
        proc = subprocess.run(  # noqa: S603
            [self.cli, "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {proc.stderr[:400]}")
        return _parse_cli_json(proc.stdout)


def _parse_cli_json(stdout: str) -> AppraisalResult:
    """Extract the JSON appraisal from the CLI's output (it wraps the reply in an envelope)."""
    text = stdout
    try:
        env = json.loads(stdout)
        text = env.get("result") or env.get("text") or stdout
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("no JSON object found in claude CLI output")
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
