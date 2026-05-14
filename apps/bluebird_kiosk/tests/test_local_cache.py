"""Unit tests for KioskLocalCache (the on-device SQLite mirror)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Make `bluebird_kiosk` importable when running from the repo root.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3]))

from bluebird_kiosk.services.local_cache import KioskLocalCache  # noqa: E402


@pytest.fixture
def cache(tmp_path: Path) -> KioskLocalCache:
    return KioskLocalCache(tmp_path / "cache.db")


def test_cursor_round_trip(cache):
    assert cache.get_cursor() is None
    cache.set_cursor("2026-05-14T01:00:00+00:00")
    assert cache.get_cursor() == "2026-05-14T01:00:00+00:00"
    cache.set_cursor("2026-05-14T02:00:00+00:00")
    assert cache.get_cursor() == "2026-05-14T02:00:00+00:00"


def test_apply_rows_upsert_and_read(cache):
    rows = [
        {"id": 1, "name": "A", "updated_at": "2026-05-14T01:00:00+00:00"},
        {"id": 2, "name": "B", "updated_at": "2026-05-14T01:01:00+00:00"},
    ]
    up, dl = cache.apply_rows("lw_albums", rows)
    assert (up, dl) == (2, 0)
    listed = cache.list_rows("lw_albums")
    assert len(listed) == 2
    assert {r["name"] for r in listed} == {"A", "B"}
    assert cache.row_count("lw_albums") == 2

    # Re-apply same row with newer cursor should upsert.
    up, dl = cache.apply_rows(
        "lw_albums",
        [{"id": 1, "name": "A renamed", "updated_at": "2026-05-14T02:00:00+00:00"}],
    )
    assert (up, dl) == (1, 0)
    fetched = cache.get_row("lw_albums", 1)
    assert fetched["name"] == "A renamed"


def test_apply_rows_tombstone_deletes(cache):
    cache.apply_rows("lw_media", [{"id": 7, "updated_at": "t0"}])
    assert cache.row_count("lw_media") == 1
    up, dl = cache.apply_rows(
        "lw_media", [{"id": 7, "deleted_at": "2026-05-14T03:00:00+00:00"}]
    )
    assert (up, dl) == (0, 1)
    assert cache.row_count("lw_media") == 0


def test_apply_rows_skips_id_less(cache):
    up, dl = cache.apply_rows(
        "lw_albums", [{"name": "no id"}, {"id": 5, "updated_at": "t"}]
    )
    assert (up, dl) == (1, 0)
    assert cache.row_count("lw_albums") == 1


def test_media_blob_track(cache, tmp_path):
    cache.record_media_blob(
        media_id=42,
        file_path=str(tmp_path / "42.bin"),
        etag='"m42-foo"',
        fetched_at="2026-05-14T04:00:00+00:00",
    )
    blob = cache.get_media_blob(42)
    assert blob is not None
    assert blob["etag"] == '"m42-foo"'
    assert blob["file_path"].endswith("42.bin")

    removed = cache.delete_media_blob(42)
    assert removed and removed.endswith("42.bin")
    assert cache.get_media_blob(42) is None
    assert cache.delete_media_blob(42) is None  # idempotent


def test_pending_events_queue_and_flush(cache):
    e1 = cache.queue_event("face_tag_submit", {"media_id": 1, "person_id": 2}, "t0")
    e2 = cache.queue_event("media_delete", {"media_id": 9}, "t1")
    assert e1 < e2
    pending = cache.list_pending_events()
    assert len(pending) == 2
    assert pending[0]["event_type"] == "face_tag_submit"
    assert pending[0]["event_data"] == {"media_id": 1, "person_id": 2}
    assert cache.pending_event_count() == 2

    removed = cache.delete_pending_events([e1])
    assert removed == 1
    assert cache.pending_event_count() == 1
    assert cache.delete_pending_events([]) == 0


def test_cache_survives_reopen(tmp_path):
    path = tmp_path / "cache.db"
    c1 = KioskLocalCache(path)
    c1.apply_rows("lw_albums", [{"id": 99, "updated_at": "t"}])
    c1.set_cursor("2026-05-14T05:00:00+00:00")

    c2 = KioskLocalCache(path)
    assert c2.get_cursor() == "2026-05-14T05:00:00+00:00"
    assert c2.row_count("lw_albums") == 1
