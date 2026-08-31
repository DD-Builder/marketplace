"""Draft the message you actually send a seller — provider-agnostic, and never auto-sent.

Two deliberate boundaries:

* **Prompt building is pure.** :func:`build_prompt` takes plain values and returns a string,
  so the negotiation logic is fully testable without a model, a network, or a cent.
* **Drafting is separate from dispatch, and dispatch does not exist.** The app produces
  candidate replies; a human reads them, edits them, and sends them from Messenger. That
  seam is where an auto-send feature would go, and it is intentionally empty.

Like the appraiser, this runs through the Claude Code CLI by default so it bills to your
Max subscription rather than a metered API key.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from dealfinder.core.schemas import NegotiationDraft, NegotiationDrafts
from dealfinder.logging import get_logger
from dealfinder.negotiation.posture import posture_params

log = get_logger(__name__)

SYSTEM = (
    "You are helping a furniture-restoration reseller negotiate with a private seller "
    "over Facebook Marketplace messenger. Write natural, concise, human messages — never "
    "robotic or template-like. Use only the leverage you're given (real condition flaws, "
    "the buyer's walk-away price). Never invent facts about the item or claim to have seen "
    "it in person unless the conversation says so."
)

_SCHEMA_HINT = '{"drafts": [{"text": str, "rationale": str}]}'


def _dollars(cents: int | None) -> str:
    return f"${cents / 100:.0f}" if cents is not None else "unknown"


def build_prompt(
    *,
    posture: int,
    listing_title: str,
    asking_price_cents: int | None = None,
    walkaway_price_cents: int | None = None,
    condition_notes: str = "",
    conversation: str = "",
) -> str:
    """Assemble the drafting prompt. Pure — no model, no I/O."""
    params = posture_params(posture)
    convo = conversation.strip() or "(no messages yet — write the opener)"
    return (
        f"{SYSTEM}\n\n"
        f"Item: {listing_title[:200]}\n"
        f"Asking price: {_dollars(asking_price_cents)}\n"
        f"My walk-away (the most I will pay): {_dollars(walkaway_price_cents)}\n"
        f"Condition notes / leverage: {condition_notes[:800] or 'none noted'}\n\n"
        f"Negotiation posture: {params.label}\n"
        f"- Anchoring: {params.anchor_guidance}\n"
        f"- Tone: {params.tone}\n"
        f"- Urgency: {params.urgency}\n"
        f"- Walk-away signalling: {params.walkaway}\n\n"
        f"Conversation so far (most recent last):\n{convo[:4000]}\n\n"
        "Write my next message. Give 2-3 short candidates I can choose between, each with "
        "a one-line rationale.\n"
        f"Return ONLY a JSON object, no prose, matching: {_SCHEMA_HINT}"
    )


class ClaudeCodeDrafter:
    """Subscription path — same CLI, same free-to-run economics, as the appraiser."""

    name = "claude-code"

    def __init__(self, cli: str = "claude", model: str = "", timeout: float = 180.0) -> None:
        self.cli = cli
        self.model = model
        self.timeout = timeout

    def draft(self, prompt: str) -> NegotiationDrafts:
        from dealfinder.appraiser import cli_failure_reason, extract_cli_json

        if not shutil.which(self.cli):
            raise RuntimeError(
                f"'{self.cli}' CLI not found. Negotiation drafting needs Claude Code "
                "installed and authenticated (locally, or via CLAUDE_CODE_OAUTH_TOKEN in CI)."
            )
        cmd = [self.cli, "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=self.timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed: {cli_failure_reason(proc.stdout, proc.stderr)}"
            )
        return NegotiationDrafts.model_validate_json(extract_cli_json(proc.stdout))


class ClaudeApiDrafter:
    """Metered-API path, for when you'd rather not use the subscription."""

    name = "claude-api"

    def __init__(self, model: str = "claude-sonnet-5", timeout: float = 120.0) -> None:
        self.model = model
        self.timeout = timeout

    def draft(self, prompt: str) -> NegotiationDrafts:
        import anthropic

        client = anthropic.Anthropic(timeout=self.timeout)
        msg = client.messages.create(
            model=self.model, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        start, end = text.find("{"), text.rfind("}")
        if start == -1:
            raise RuntimeError(f"no JSON in the reply: {text[:200]!r}")
        return NegotiationDrafts.model_validate_json(text[start : end + 1])


_DRAFTERS = {
    "claude-code": ClaudeCodeDrafter,
    "claude-api": ClaudeApiDrafter,
}


def get_drafter(name: str = "claude-code"):
    """Pick a drafting driver by name — the same seam the appraiser uses."""
    import os

    if name not in _DRAFTERS:
        raise ValueError(
            f"unknown drafter {name!r}; available: {', '.join(sorted(_DRAFTERS))}"
        )
    if name == "claude-code":
        # Empty = the CLI's default model. NEGOTIATION_MODEL pins it.
        return ClaudeCodeDrafter(model=(os.getenv("NEGOTIATION_MODEL") or "").strip())
    return _DRAFTERS[name]()


def draft_replies(
    *,
    posture: int,
    listing_title: str,
    asking_price_cents: int | None = None,
    walkaway_price_cents: int | None = None,
    condition_notes: str = "",
    conversation: str = "",
    drafter=None,
) -> NegotiationDrafts:
    """Candidate replies tuned to the posture. Nothing is sent anywhere."""
    prompt = build_prompt(
        posture=posture, listing_title=listing_title,
        asking_price_cents=asking_price_cents,
        walkaway_price_cents=walkaway_price_cents,
        condition_notes=condition_notes, conversation=conversation,
    )
    drafts = (drafter or get_drafter()).draft(prompt)
    if not drafts.drafts:
        raise RuntimeError("draft generation returned no candidates")
    return NegotiationDrafts(
        drafts=[NegotiationDraft(text=d.text.strip(), rationale=d.rationale.strip())
                for d in drafts.drafts]
    )


_MONEY = re.compile(r"\$\s?(\d[\d,]*)(?:\.(\d{2}))?")


def offers_above(text: str, walkaway_price_cents: int | None) -> list[int]:
    """Dollar figures in a draft that exceed your walk-away price, in cents.

    The one failure mode that would make this feature actively harmful is a draft that
    cheerfully offers more than you're willing to pay. It's flagged rather than hidden,
    because the model may be quoting the seller's number back at them — you decide.
    """
    if walkaway_price_cents is None:
        return []
    out = []
    for whole, frac in _MONEY.findall(text):
        cents = int(whole.replace(",", "")) * 100 + int(frac or 0)
        if cents > walkaway_price_cents:
            out.append(cents)
    return out
