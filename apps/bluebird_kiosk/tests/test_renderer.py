"""Tests for the local Legacy Wall renderer.

Covers:
  - collect_slideshow_media filtering + ordering
  - resolve_media_file_path
  - HTTP endpoints (/legacy-wall, /legacy-wall/api/media, /legacy-wall/media/{id})
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3]))

from bluebird_kiosk.services.local_cache import KioskLocalCache  # noqa: E402
from bluebird_kiosk.services.renderer import (  # noqa: E402
    collect_slideshow_media,
    resolve_media_file_path,
)


# ── Unit tests for renderer helpers ──────────────────────────────────────────


def _seed_media(cache: KioskLocalCache, media_dir: Path, items):
    """items: list of dicts with id, optional caption / published / taken_at,
    and a `with_blob` flag. Writes a tiny placeholder blob when requested."""
    media_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in items:
        rows.append(
            {
                "id": item["id"],
                "caption": item.get("caption", ""),
                "alt_text": item.get("alt_text", ""),
                "taken_at": item.get("taken_at", ""),
                "published": item.get("published", True),
                "updated_at": item.get("updated_at", "t0"),
            }
        )
    cache.apply_rows("lw_media", rows)
    for item in items:
        if item.get("with_blob", True):
            blob_path = media_dir / f'{item["id"]}.bin'
            blob_path.write_bytes(b"FAKEJPEG")
            cache.record_media_blob(
                item["id"], str(blob_path), '"e"', "2026-05-14T00:00:00+00:00"
            )


def test_collect_slideshow_media_orders_newest_first(tmp_path):
    cache = KioskLocalCache(tmp_path / "cache.db")
    _seed_media(
        cache,
        tmp_path / "media",
        [
            {"id": 1, "caption": "Old", "taken_at": "2024-01-01T00:00:00Z"},
            {"id": 2, "caption": "New", "taken_at": "2026-01-01T00:00:00Z"},
            {"id": 3, "caption": "Mid", "taken_at": "2025-06-01T00:00:00Z"},
        ],
    )
    media = collect_slideshow_media(cache)
    assert [m["id"] for m in media] == [2, 3, 1]
    assert media[0]["caption"] == "New"


def test_collect_slideshow_media_drops_unpublished(tmp_path):
    cache = KioskLocalCache(tmp_path / "cache.db")
    _seed_media(
        cache,
        tmp_path / "media",
        [
            {"id": 1, "caption": "Shown"},
            {"id": 2, "caption": "Hidden", "published": False},
        ],
    )
    ids = [m["id"] for m in collect_slideshow_media(cache)]
    assert ids == [1]


def test_collect_slideshow_media_drops_when_no_blob(tmp_path):
    cache = KioskLocalCache(tmp_path / "cache.db")
    _seed_media(
        cache,
        tmp_path / "media",
        [
            {"id": 1, "with_blob": True},
            {"id": 2, "with_blob": False},
        ],
    )
    ids = [m["id"] for m in collect_slideshow_media(cache)]
    assert ids == [1]


def test_resolve_media_file_path(tmp_path):
    cache = KioskLocalCache(tmp_path / "cache.db")
    _seed_media(cache, tmp_path / "media", [{"id": 42}])
    path = resolve_media_file_path(cache, 42)
    assert path and Path(path).is_file()
    assert resolve_media_file_path(cache, 9999) is None


# ── HTTP endpoint tests ──────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.db"
    monkeypatch.setenv("BLUEBIRD_LOCAL_CACHE_DB", str(cache_path))
    monkeypatch.setenv("BLUEBIRD_KIOSK_CONF", str(tmp_path / "kiosk.conf"))
    monkeypatch.setenv("BLUEBIRD_LICENSE_TOKEN", str(tmp_path / "license.token"))
    # Pre-create kiosk.conf so config.read_config returns the tenant name.
    (tmp_path / "kiosk.conf").write_text(
        'BLUEBIRD_BACKEND=https://example\n'
        'SCHOOL_SLUG=demo\n'
        'TENANT_NAME=Demo School\n',
        encoding="utf-8",
    )
    # Build an app instance using the env-overridden paths. Reload both
    # server and config since they capture env-driven paths at import time.
    for mod_name in (
        "bluebird_kiosk.server",
        "bluebird_kiosk.config",
    ):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    from bluebird_kiosk.server import create_app  # noqa: WPS433
    app = create_app()
    _seed_media(
        app.state.local_cache,
        tmp_path / "media",
        [{"id": 1, "caption": "Hello", "taken_at": "2026-01-01T00:00:00Z"}],
    )
    return TestClient(app), app, tmp_path


def test_legacy_wall_home_renders(client):
    c, _, _ = client
    resp = c.get("/legacy-wall")
    assert resp.status_code == 200
    body = resp.text
    # Structural assertions — the page renders the slideshow shell. We don't
    # check the tenant name because config.read_config caches CONF_PATH at
    # module-import time, which makes that assertion order-dependent in tests.
    assert "lw-slot" in body
    assert "/static/legacy_wall.js" in body
    assert "/static/legacy_wall.css" in body


def test_legacy_wall_api_media(client):
    c, _, _ = client
    resp = c.get("/legacy-wall/api/media")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["media"][0]["id"] == 1
    assert data["media"][0]["caption"] == "Hello"


def test_legacy_wall_media_blob(client):
    c, _, _ = client
    resp = c.get("/legacy-wall/media/1")
    assert resp.status_code == 200
    assert resp.content == b"FAKEJPEG"


def test_legacy_wall_media_blob_unknown_404(client):
    c, _, _ = client
    resp = c.get("/legacy-wall/media/9999")
    assert resp.status_code == 404
