"""Test fixtures: an isolated temp SQLite DB and fixture-loading helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite DB and create the schema."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Reset cached settings + the module-level engine so they pick up the temp URL.
    from dealfinder import config
    from dealfinder.core import db as db_mod

    config.get_settings.cache_clear()
    db_mod._engine = None
    db_mod._SessionLocal = None
    db_mod.init_db()
    yield db_mod
    db_mod._engine = None
    db_mod._SessionLocal = None
    config.get_settings.cache_clear()


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()
