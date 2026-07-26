"""The photo pipeline: the downloader's circuit breaker, orphan reconciliation, and the
gallery store. The live board shipped with zero photographs on a furniture-buying tool —
every appraised entry had only expired CDN URLs while 20 paid-for image files sat on
disk linked to nothing. These pin the pieces of the fix.
"""

from __future__ import annotations

import io
from pathlib import Path

from dealfinder import catalog as cat
from dealfinder import run_board
from dealfinder.core.schemas import RawListing, RawPhoto


def _listing(id_: str, n_photos: int = 3) -> RawListing:
    return RawListing(
        fb_listing_id=id_,
        title=f"dresser {id_}",
        asking_price_cents=10000,
        photos=[RawPhoto(remote_url=f"https://cdn.example/{id_}/{i}.jpg", position=i)
                for i in range(n_photos)],
    )


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_gives_up_after_six_consecutive_failures(monkeypatch, tmp_path):
    """A network that can't reach the CDN must not burn the job's 45-minute budget
    retrying every URL of every listing."""
    attempts = []

    def dead_cdn(req, timeout=0):
        attempts.append(req.full_url)
        raise OSError("unreachable")

    monkeypatch.setattr(run_board.urllib.request, "urlopen", dead_cdn)
    got = run_board._download_photos([_listing(f"l{i}") for i in range(20)], tmp_path)
    assert got == {}
    # One attempt per listing (the inner loop breaks on first failure), six before the
    # breaker opens; everything after is skipped without a request.
    assert len(attempts) == 6


def test_one_success_resets_the_breaker(monkeypatch, tmp_path):
    calls = {"n": 0}

    def flaky(req, timeout=0):
        calls["n"] += 1
        if "l5" in req.full_url:                      # only listing l5 has a live URL
            return _FakeResponse(b"jpg")
        raise OSError("unreachable")

    monkeypatch.setattr(run_board.urllib.request, "urlopen", flaky)
    listings = [_listing(f"l{i}", n_photos=1) for i in range(10)]
    got = run_board._download_photos(listings, tmp_path)
    assert list(got) == ["l5"]
    # l0-l4 fail (5 strikes), l5 succeeds and resets, l6-l9 fail (4 strikes) — the
    # breaker never opens, so every listing got its attempt.
    assert calls["n"] == 10


def test_reconcile_links_orphaned_photos_and_removes_strays(tmp_path):
    """The exact live-board state: files on disk, photo_rel None, and one file whose
    listing no longer exists."""
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "111.jpg").write_bytes(b"x")
    (photos / "111_1.jpg").write_bytes(b"x")
    (photos / "999.jpg").write_bytes(b"x")            # no such listing any more

    c = cat.Catalog()
    cat.observe(c, [_listing("111")])
    assert c.listings["111"].photo_rel is None

    linked = run_board._reconcile_photos(c, photos)
    assert linked == 1
    assert c.listings["111"].photo_rel == "photos/111.jpg"
    assert c.listings["111"].extra_photo_rels == ["photos/111_1.jpg"]
    assert not (photos / "999.jpg").exists()
    assert (photos / "111.jpg").exists()

    # Idempotent: a second pass changes nothing and deletes nothing.
    assert run_board._reconcile_photos(c, photos) == 0
    assert c.listings["111"].photo_rel == "photos/111.jpg"


def test_gallery_shots_survive_instead_of_being_paid_for_and_discarded(tmp_path):
    src = tmp_path / "_photos"
    src.mkdir()
    paths = []
    for i in range(3):
        p = src / f"111_{i}.jpg"
        p.write_bytes(b"jpg")
        paths.append(p)

    c = cat.Catalog()
    cat.observe(c, [_listing("111")])
    run_board._store_extra_photos(c, {"111": paths}, tmp_path / "photos")

    assert c.listings["111"].extra_photo_rels == ["photos/111_1.jpg", "photos/111_2.jpg"]
    assert (tmp_path / "photos" / "111_1.jpg").exists()
    assert (tmp_path / "photos" / "111_2.jpg").exists()


def test_search_urls_survive_commas_inside_a_url():
    urls = run_board._search_urls(
        "https://www.facebook.com/marketplace/lex/search/?query=a,b\n"
        "https://www.facebook.com/marketplace/lex/search/?query=x, "
        "https://www.facebook.com/marketplace/lex/search/?query=y"
    )
    assert urls == [
        "https://www.facebook.com/marketplace/lex/search/?query=a,b",
        "https://www.facebook.com/marketplace/lex/search/?query=x",
        "https://www.facebook.com/marketplace/lex/search/?query=y",
    ]
