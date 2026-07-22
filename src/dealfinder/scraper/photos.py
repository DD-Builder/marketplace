"""Download listing photos, content-addressed by sha256 (dedups relists)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from dealfinder.config import get_settings
from dealfinder.logging import get_logger

log = get_logger(__name__)

_EXT_BY_CT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _ext_for(content_type: str, url: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    if ct in _EXT_BY_CT:
        return _EXT_BY_CT[ct]
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        if ext in url.lower():
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


async def download_photo(client: httpx.AsyncClient, url: str) -> tuple[Path, str] | None:
    """Download one photo. Returns (local_path, sha256) or None on failure."""
    try:
        resp = await client.get(url, timeout=30.0)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("photo_download_failed", url=url[:120], error=str(exc))
        return None

    data = resp.content
    digest = hashlib.sha256(data).hexdigest()
    ext = _ext_for(resp.headers.get("content-type", ""), url)
    dest = get_settings().photo_path / f"{digest}{ext}"
    if not dest.exists():
        dest.write_bytes(data)
    return dest, digest


async def download_all(urls: list[str]) -> list[tuple[Path, str]]:
    """Download a listing's photos concurrently, preserving order."""
    if not urls:
        return []
    results: list[tuple[Path, str] | None] = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for url in urls:  # sequential keeps it gentle; volumes are small per listing
            results.append(await download_photo(client, url))
    return [r for r in results if r is not None]
