"""EBTH's own category taxonomy, and how it maps onto our pricing verticals.

Discovering by *their* categories rather than our guessed keywords is strictly better:
a keyword query can only ever find lots whose text happens to contain the word, while
``category_id`` returns everything the house itself filed under that heading — including
the lot titled "Estate Lot, Assorted" that no keyword would ever surface.

The IDs and slugs here were read off the live site (the ``filters`` block of the
/browse response names the query parameter as ``category_id`` and carries the whole
tree); they are not invented. Only the top level is modelled — EBTH nests several levels
deep, and a top-level id already returns everything beneath it.

Each category also names the vertical whose pricing rules and appraiser guidance should
govern the lots it yields, which is what lets "Jewelry and Watches" be appraised as
jewelry rather than as furniture.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    id: int
    label: str          # exactly what EBTH calls it, so the UI matches their site
    slug: str
    vertical: str       # which of our verticals prices it
    #: False for categories we have no honest way to value — appliances, vehicles, pet
    #: supplies. They stay listed (so the taxonomy is complete and selectable) but are
    #: not trawled by default, because an appraisal we can't stand behind is worse than
    #: no appraisal.
    default_on: bool = True


#: The live top-level tree, in EBTH's own order and wording.
CATEGORIES: tuple[Category, ...] = (
    Category(3313, "Jewelry and Watches", "jewelry-and-watches", "jewelry"),
    Category(3472, "Furniture", "furniture", "furniture"),
    Category(3013, "Art", "art", "art"),
    Category(3556, "Decor", "decor", "decor"),
    Category(3094, "Collectibles", "collectibles", "collectibles"),
    Category(4489, "Lighting", "lighting", "decor"),
    Category(3595, "Kitchen and Housewares", "kitchen-and-housewares", "decor"),
    Category(3892, "Sports and Entertainment", "sports-and-entertainment", "collectibles"),
    Category(3799, "Music and Instruments", "music-and-instruments", "collectibles"),
    Category(3187, "Electronics and Computers", "electronics-and-computers", "electronics"),
    Category(3082, "Books, Maps, Manuscripts & Ephemera",
             "books-maps-manuscripts-and-ephemera", "collectibles"),
    Category(3259, "Fashion and Accessories", "fashion-and-accessories", "collectibles"),
    Category(4084, "Toys and Games", "toys-and-games", "collectibles"),
    Category(4153, "Entertainment", "entertainment", "collectibles"),
    Category(4498, "Home Improvement", "home-improvement", "decor", default_on=False),
    Category(4483, "Outdoor and Garden", "outdoor-and-garden", "decor", default_on=False),
    Category(4480, "Bed and Bath", "bed-and-bath", "decor", default_on=False),
    Category(3391, "Appliances", "appliances", "decor", default_on=False),
    Category(3064, "Automotive", "automotive", "collectibles", default_on=False),
)

BY_ID = {c.id: c for c in CATEGORIES}
BY_SLUG = {c.slug: c for c in CATEGORIES}

#: EBTH's own sort presets, read from the live ``sorts`` block. Using theirs rather than
#: sorting our own page means the *server* decides which lots we even see — which is the
#: only way to reach the hottest lots in a category that runs to thousands of items.
SORTS = {
    "ending_soon": "sale_ends_at_asc",
    "highest_bid": "price_desc",
    "most_bids": "bids",
    "most_followed": "follows",
    "recommended": "recommended",
    "newest": "sale_starts_at_desc",
}


def resolve(names: str) -> list[Category]:
    """Parse a user-supplied category list (ids, slugs, or names) into categories.

    Empty selects the default-on set. Unknown entries are skipped rather than fatal: a
    typo in a repository variable should narrow the trawl, not break the hourly run.
    """
    raw = [p.strip() for p in names.replace("\n", ",").split(",") if p.strip()]
    if not raw:
        return [c for c in CATEGORIES if c.default_on]
    out: list[Category] = []
    for token in raw:
        if token.isdigit() and int(token) in BY_ID:
            out.append(BY_ID[int(token)])
            continue
        key = token.lower().replace(" ", "-").replace("&", "and")
        if key in BY_SLUG:
            out.append(BY_SLUG[key])
            continue
        match = next(
            (c for c in CATEGORIES if c.label.lower() == token.lower()), None
        )
        if match:
            out.append(match)
    # Dedupe, preserving the order given.
    seen: set[int] = set()
    return [c for c in out if not (c.id in seen or seen.add(c.id))]
