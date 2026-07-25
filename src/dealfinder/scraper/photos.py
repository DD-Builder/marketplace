"""Store listing photos, content-addressed by sha256 (dedups relists).

Bytes are fetched through the browser context (see ``BrowserSession.fetch_bytes``) so the
CDN sees the page's cookies/UA — a bare HTTP client gets 403'd by scontent (finding P3).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from dealfinder.config import get_settings
from dealfinder.logging import get_logger

log = get_logger(__name__)

_MAGIC_EXT = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG": ".png",
    b"GIF8": ".gif",
    b"RIFF": ".webp",  # RIFF....WEBP
}


class BytesFetcher(Protocol):
    async def fetch_bytes(self, url: str) -> bytes | None:  # pragma: no cover
        ...


def _ext_for(data: bytes) -> str:
    for magic, ext in _MAGIC_EXT.items():
        if data.startswith(magic):
            return ext
    return ".jpg"


def store_bytes(data: bytes) -> tuple[Path, str]:
    """Write image bytes content-addressed by sha256; return (path, digest)."""
    digest = hashlib.sha256(data).hexdigest()
    dest = get_settings().photo_path / f"{digest}{_ext_for(data)}"
    if not dest.exists():
        dest.write_bytes(data)
    return dest, digest


async def fetch_and_store(session: BytesFetcher, url: str) -> tuple[Path, str] | None:
    """Fetch one photo via the browser session and store it. None on failure."""
    data = await session.fetch_bytes(url)
    if not data:
        return None
    return store_bytes(data)
