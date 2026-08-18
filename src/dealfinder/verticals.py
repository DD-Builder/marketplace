"""Pluggable *verticals* — the per-category knowledge the funnel is tuned to.

A vertical bundles everything that changes when you hunt a different kind of thing:
the keyword signals the free pre-screen uses, the sane price window, and the guidance
handed to the AI appraiser. Furniture is the first, but art / electronics / plants /
whatever are just another :class:`Vertical` in the registry — nothing downstream
(dedup, selection, scoring, resale) is furniture-specific.

Add a vertical by defining it and dropping it in ``_REGISTRY``; the scrape target then
names it by ``key``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Vertical:
    key: str
    label: str
    # Words that signal a piece is plausibly worth acquiring/reselling.
    positive: frozenset[str] = frozenset()
    # Known makers/brands that punch above generic keywords.
    makers: frozenset[str] = frozenset()
    # Words that signal low-value / non-restorable / fast-goods junk.
    negative: frozenset[str] = frozenset()
    # Price sanity window (cents). Below min → typo/scam/free-pile; above max → not a flip.
    min_price_cents: int = 500
    max_price_cents: int = 300_000
    # Injected into the appraiser's system prompt so valuation is category-aware.
    appraiser_guidance: str = ""


FURNITURE = Vertical(
    key="furniture",
    label="Furniture (restore & flip)",
    positive=frozenset({
        "solid wood", "solid oak", "teak", "walnut", "mahogany", "rosewood", "oak",
        "maple", "cherry", "mid century", "mid-century", "mcm", "danish", "antique",
        "vintage", "dovetail", "brass", "marble", "cast iron", "wrought iron", "burl",
        "handmade", "hardwood", "art deco",
    }),
    makers=frozenset({
        "eames", "herman miller", "knoll", "drexel", "henredon", "broyhill", "brasilia",
        "thomasville", "stickley", "ercol", "g plan", "g-plan", "lane", "dux", "baker",
        "kittinger", "widdicomb", "heywood", "bassett", "milo baughman", "kroehler",
    }),
    negative=frozenset({
        "ikea", "particle board", "particleboard", "mdf", "laminate", "faux",
        "pressboard", "press board", "wayfair", "fast furniture", "flat pack",
        "flat-pack", "melamine", "veneer peeling", "water damaged beyond", "mold",
    }),
    appraiser_guidance=(
        "This is a furniture piece being considered for restoration and resale. Judge "
        "construction (solid wood vs. veneer vs. particleboard), joinery (dovetails, "
        "mortise-and-tenon), maker marks, era, and what restoration it realistically needs."
    ),
)

ART = Vertical(
    key="art",
    label="Art & prints",
    positive=frozenset({
        "original", "oil on canvas", "signed", "listed artist", "lithograph", "etching",
        "serigraph", "watercolor", "mid century", "vintage", "framed", "limited edition",
    }),
    makers=frozenset(),  # artist-name matching is better handled by the appraiser than a keyword set
    negative=frozenset({
        "poster", "print of", "reproduction", "canvas print", "ikea", "mass produced",
        "wall decor", "hobby lobby", "print copy",
    }),
    min_price_cents=1000,
    max_price_cents=500_000,
    appraiser_guidance=(
        "This is a work of art. Distinguish an original (oil/acrylic/works-on-paper) or a "
        "genuine limited print from a mass-market reproduction. Note signature, medium, "
        "and any listed-artist attribution — but be conservative: most 'art' is decor."
    ),
)

ELECTRONICS = Vertical(
    key="electronics",
    label="Vintage electronics & audio",
    positive=frozenset({
        "vintage", "tube", "receiver", "turntable", "amplifier", "reel to reel",
        "marantz", "pioneer", "sansui", "mcintosh", "technics", "klipsch", "working",
        "tested", "serviced", "recapped",
    }),
    makers=frozenset({
        "marantz", "mcintosh", "sansui", "pioneer", "technics", "klipsch", "nakamichi",
        "harman kardon", "fisher", "scott", "luxman", "thorens",
    }),
    negative=frozenset({
        "as is not working", "for parts", "no cord", "broken screen", "cracked",
        "water damage", "won't power", "does not turn on", "untested sold as is",
    }),
    min_price_cents=1000,
    max_price_cents=800_000,
    appraiser_guidance=(
        "This is a piece of consumer electronics/audio. Working condition is decisive — a "
        "non-functional unit is worth parts only. Weigh brand desirability (vintage hi-fi "
        "commands a premium), whether it's been serviced/recapped, and repair cost/risk."
    ),
)


JEWELRY = Vertical(
    key="jewelry",
    label="Fine & estate jewelry",
    positive=frozenset({
        "sterling", "sterling silver", "14k", "18k", "24k", "gold", "platinum",
        "diamond", "gemstone", "ruby", "sapphire", "emerald", "pearl", "vintage",
        "antique", "art deco", "victorian", "edwardian", "estate", "hallmark",
        "hallmarked", "signed",
    }),
    makers=frozenset({
        "tiffany", "cartier", "bulgari", "van cleef", "harry winston", "david yurman",
        "chanel", "buccellati", "mikimoto",
    }),
    negative=frozenset({
        "costume jewelry", "costume", "gold plated", "silver plated", "gold tone",
        "silver tone", "faux", "cz", "cubic zirconia", "rhinestone", "fashion jewelry",
    }),
    min_price_cents=1000,
    max_price_cents=1_000_000,
    appraiser_guidance=(
        "This is a piece of jewelry. Judge metal purity from hallmarks/stamps (sterling, "
        "14k/18k/platinum), whether stones read as natural or simulant, maker marks, and "
        "era. Costume/fashion jewelry — plated, unmarked, cubic zirconia — is low value "
        "regardless of how it photographs."
    ),
)

COLLECTIBLES = Vertical(
    key="collectibles",
    label="Silver, coins, watches & fine collectibles",
    positive=frozenset({
        "sterling silver", "sterling", "coin silver", "hallmark", "hallmarked",
        "hand-knotted", "hand knotted", "wool", "silk", "porcelain", "hand painted",
        "antique", "vintage", "asian", "chinese", "japanese", "swiss made",
        "automatic movement", "chronograph", "estate", "signed", "numbered",
    }),
    makers=frozenset({
        "rolex", "omega", "patek philippe", "cartier", "tiffany", "gorham",
        "reed & barton", "reed and barton", "international silver", "wallace", "towle",
        "georg jensen", "christofle",
    }),
    negative=frozenset({
        "silverplate", "silver plate", "plated flatware", "reproduction", "replica",
        "costume", "made in china unmarked", "quartz fashion watch",
    }),
    min_price_cents=1000,
    max_price_cents=2_000_000,
    appraiser_guidance=(
        "This is a collectible — silver, a coin, a watch, a rug, or Asian/decorative art. "
        "Judge material purity (sterling hallmarks vs. plate), maker marks, movement type "
        "and originality for watches, knot density/dye/wear for rugs, and condition. "
        "Reproductions and silverplate are low value regardless of apparent age."
    ),
)


_REGISTRY: dict[str, Vertical] = {
    v.key: v for v in (FURNITURE, ART, ELECTRONICS, JEWELRY, COLLECTIBLES)
}

DEFAULT_VERTICAL = FURNITURE


def get_vertical(key: str | None) -> Vertical:
    """Look up a vertical by key, falling back to the default (furniture)."""
    if not key:
        return DEFAULT_VERTICAL
    return _REGISTRY.get(key.lower().strip(), DEFAULT_VERTICAL)


def all_verticals() -> list[Vertical]:
    return list(_REGISTRY.values())


def register(vertical: Vertical) -> None:
    """Add or replace a vertical at runtime (e.g. a user-defined niche)."""
    _REGISTRY[vertical.key] = vertical
