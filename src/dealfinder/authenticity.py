"""Fake / knock-off detection — the loud red flag before you get excited about a maker.

Sellers constantly hedge: "Eames-esque lounge chair", "Barcelona style", "in the manner
of Wegner", "reproduction Tulip table". Every one of those is a tell that the piece is
*styled after* a famous design, not the genuine article — and it must never be valued as
the real thing. This scans the title/description for three tiers of tell:

* **Reproduction words** ("replica", "repro", "knockoff", "dupe") — an explicit admission.
* **Designer-name + qualifier** ("Eames" near "style"/"-esque") — the classic dodge.
* **Attribution hedges** ("attributed to", "unmarked", "we think") — genuine *maybe*, but
  the maker is unconfirmed, so don't pay the confirmed-maker price.

Pure text rules, zero cost. It doesn't reject the listing — a cheap look-alike can still be
a flip at look-alike prices — it reclassifies how the piece should be *valued* and raises a
visible warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from dealfinder.core.schemas import RawListing

# Explicit "this is a copy" admissions — always a red flag on their own.
_REPRODUCTION = {
    "replica", "reproduction", "repro", "knockoff", "knock off", "knock-off",
    "dupe", "lookalike", "look alike", "look-alike", "imitation", "copy of",
    "not authentic", "not genuine", "not original",
}
# Styling qualifiers — dangerous when they sit next to a famous name.
_QUALIFIERS = {
    "style", "styled", "inspired", "inspired by", "in the style of", "manner of",
    "tribute", "homage", "type", "after",
}
# Famous designs/designers that get knocked off. Name-adjacent qualifier = look-alike.
_DESIGN_NAMES = {
    "eames", "herman miller", "barcelona", "mies", "le corbusier", "corbusier",
    "wegner", "wishbone", "noguchi", "saarinen", "tulip", "egg chair", "swan chair",
    "jacobsen", "womb chair", "panton", "bertoia", "cesca", "breuer", "wassily",
    "george nelson", "nelson bench", "ghost chair", "starck", "acapulco", "platner",
    "florence knoll", "knoll", "thonet", "chesterfield", "aeron", "florence",
}
# Attribution hedges — maker unconfirmed, value as such.
_HEDGES = {
    "attributed to", "believed to be", "possibly", "unmarked", "no markings",
    "unsigned", "we think", "appears to be", "might be", "could be", "unconfirmed",
}


@lru_cache(maxsize=None)
def _re(term: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(term)}\b")


def _spans(text: str, terms: set[str]) -> list[tuple[int, int, str]]:
    out = []
    for t in terms:
        for m in _re(t).finditer(text):
            out.append((m.start(), m.end(), t))
    return out


# "eames-esque", "danish-esque", "chairesque" — the suffix literally means "in the style of".
_ESQUE_RE = re.compile(r"\b(\w+?)[-\s]?esque\b")

# How close a designer name and a qualifier must sit to read as "<name> style".
_NEAR = 24


@dataclass
class AuthenticityAssessment:
    verdict: str                 # reproduction | styled_after | generic_style | hedged | clear
    is_red_flag: bool            # loud warning: do NOT value as the genuine article
    value_basis: str             # lookalike | unconfirmed | genuine_ok
    warnings: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)


def assess_authenticity(listing: RawListing) -> AuthenticityAssessment:
    """Scan a listing's text for fake / styled-after / hedged-attribution tells."""
    text = f"{listing.title}\n{listing.description}".lower()

    repro = _spans(text, _REPRODUCTION)
    if repro:
        words = sorted({t for *_, t in repro})
        return AuthenticityAssessment(
            verdict="reproduction",
            is_red_flag=True,
            value_basis="lookalike",
            warnings=[f"Seller calls it a {' / '.join(words)} — value as a copy, not the original."],
            matched=words,
        )

    name_spans = _spans(text, _DESIGN_NAMES)
    qual_spans = _spans(text, _QUALIFIERS)
    esque = list(_ESQUE_RE.finditer(text))

    # Designer name sitting next to a styling qualifier -> "<name> style".
    styled_pairs: list[str] = []
    for ns, ne, name in name_spans:
        for qs, qe, qual in qual_spans:
            if min(abs(qs - ne), abs(ns - qe)) <= _NEAR:
                styled_pairs.append(f"'{name}' described as '{qual}'")
    # "<name>-esque" — name captured by the esque group, or a name near an esque hit.
    for m in esque:
        stem = m.group(1)
        if stem in _DESIGN_NAMES or any(
            abs(m.start() - ns) <= _NEAR for ns, _, _ in name_spans
        ):
            styled_pairs.append(f"'{m.group(0)}'")

    if styled_pairs:
        uniq = sorted(set(styled_pairs))
        return AuthenticityAssessment(
            verdict="styled_after",
            is_red_flag=True,
            value_basis="lookalike",
            warnings=[
                "Styled after a famous design, not the genuine article "
                f"({'; '.join(uniq)}). Value as a look-alike."
            ],
            matched=uniq,
        )

    # A bare "-esque" / "style" with no designer named — generic descriptor, soft note.
    if esque or qual_spans:
        marks = sorted({m.group(0) for m in esque} | {t for *_, t in qual_spans})
        return AuthenticityAssessment(
            verdict="generic_style",
            is_red_flag=False,
            value_basis="genuine_ok",
            warnings=["Generic 'style' wording; treat as an unbranded piece, not a named design."],
            matched=marks,
        )

    hedges = _spans(text, _HEDGES)
    if hedges:
        words = sorted({t for *_, t in hedges})
        return AuthenticityAssessment(
            verdict="hedged",
            is_red_flag=False,
            value_basis="unconfirmed",
            warnings=[f"Seller hedges the attribution ({', '.join(words)}) — maker unconfirmed."],
            matched=words,
        )

    return AuthenticityAssessment(verdict="clear", is_red_flag=False, value_basis="genuine_ok")
