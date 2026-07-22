"""Shared FastAPI dependencies and template setup."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _cents_to_dollars(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"${cents / 100:,.0f}"


def _score_color(score: float | None) -> str:
    if score is None:
        return "#9a9aa2"
    if score >= 60:
        return "#2e7d32"
    if score >= 30:
        return "#b5651d"
    return "#b23b3b"


def _photo_url(local_path: str | None) -> str:
    if not local_path:
        return ""
    return "/photos/" + Path(local_path).name


templates.env.filters["dollars"] = _cents_to_dollars
templates.env.filters["score_color"] = _score_color
templates.env.filters["photo_url"] = _photo_url
