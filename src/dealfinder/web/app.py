"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from dealfinder.core.db import init_db
from dealfinder.web.routes import feed, listing, negotiation, status, targets

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Deal Finder", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.mount(
        "/photos",
        StaticFiles(directory=_photo_dir()),
        name="photos",
    )
    app.include_router(feed.router)
    app.include_router(listing.router)
    app.include_router(targets.router)
    app.include_router(negotiation.router)
    app.include_router(status.router)
    return app


def _photo_dir() -> str:
    from dealfinder.config import get_settings

    return str(get_settings().photo_path)


app = create_app()
