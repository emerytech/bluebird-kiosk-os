"""Unit tests for SyncClient — the three-phase loop (pull / fetch / push).

We don't spin up a real backend; we mock requests.Session so each tick is
deterministic and runs in milliseconds.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3]))

from bluebird_kiosk.services.local_cache import KioskLocalCache  # noqa: E402
from bluebird_kiosk.services.sync_client import SyncClient  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_body: Optional[Dict[str, Any]] = None,
        body_bytes: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.status_code = status_code
        self._json = json_body
        self._bytes = body_bytes or b""
        self.headers = headers or {}
        self.text = "" if json_body is None else "<json>"

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size: int = 0):
        yield self._bytes


def _make_client(tmp_path: Path, session: MagicMock) -> SyncClient:
    cache = KioskLocalCache(tmp_path / "cache.db")
    return SyncClient(
        backend="https://backend.example",
        token="tok",
        cache=cache,
        media_dir=tmp_path / "media",
        session=session,
    )


# ── pull_manifest ────────────────────────────────────────────────────────────


def test_pull_manifest_initial_pull_advances_cursor(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    session.get.return_value = FakeResponse(
        json_body={
            "ok": True,
            "cursor_at": "2026-05-14T01:00:00+00:00",
            "rows": {
                "lw_albums": [
                    {"id": 1, "name": "Album A", "updated_at": "2026-05-14T00:30:00+00:00"}
                ],
                "lw_media": [
                    {"id": 11, "file_path": "/srv/lw/1.jpg", "updated_at": "2026-05-14T00:30:00+00:00"}
                ],
            },
        }
    )
    client = _make_client(tmp_path, session)
    stats = client.pull_manifest()

    assert stats["ok"] is True
    assert stats["rows_upserted"] == 2
    assert stats["rows_deleted"] == 0
    assert client.cache.get_cursor() == "2026-05-14T01:00:00+00:00"
    # Was the `since` param empty on the first call?
    _, kwargs = session.get.call_args
    assert kwargs["params"] == {"since": ""}


def test_pull_manifest_subsequent_pull_sends_cursor(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    session.get.return_value = FakeResponse(
        json_body={
            "ok": True,
            "cursor_at": "2026-05-14T02:00:00+00:00",
            "rows": {},
        }
    )
    client = _make_client(tmp_path, session)
    client.cache.set_cursor("2026-05-14T01:00:00+00:00")
    stats = client.pull_manifest()
    assert stats["ok"] is True
    _, kwargs = session.get.call_args
    assert kwargs["params"] == {"since": "2026-05-14T01:00:00+00:00"}


def test_pull_manifest_tombstone_deletes_locally(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    # Seed: pull a media row first.
    session.get.return_value = FakeResponse(
        json_body={
            "cursor_at": "t1",
            "rows": {"lw_media": [{"id": 5, "updated_at": "t1"}]},
        }
    )
    client = _make_client(tmp_path, session)
    client.pull_manifest()
    assert client.cache.row_count("lw_media") == 1
    # Pretend we'd also downloaded the blob.
    blob_path = client.media_dir / "5.bin"
    blob_path.write_bytes(b"x")
    client.cache.record_media_blob(5, str(blob_path), '"e"', "t1")

    # Second manifest: tombstone.
    session.get.return_value = FakeResponse(
        json_body={
            "cursor_at": "t2",
            "rows": {"lw_media": [{"id": 5, "deleted_at": "t2"}]},
        }
    )
    stats = client.pull_manifest()
    assert stats["rows_deleted"] == 1
    assert client.cache.row_count("lw_media") == 0
    assert client.cache.get_media_blob(5) is None
    assert not blob_path.exists()


def test_pull_manifest_unauthorized_does_not_advance_cursor(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    session.get.return_value = FakeResponse(status_code=401)
    client = _make_client(tmp_path, session)
    client.cache.set_cursor("2026-05-14T01:00:00+00:00")
    stats = client.pull_manifest()
    assert stats["ok"] is False
    assert stats["error"] == "unauthorized"
    # Cursor unchanged.
    assert client.cache.get_cursor() == "2026-05-14T01:00:00+00:00"


def test_pull_manifest_network_error(tmp_path: Path):
    import requests as real_requests

    session = MagicMock()
    session.headers = {}
    session.get.side_effect = real_requests.ConnectionError("boom")
    client = _make_client(tmp_path, session)
    stats = client.pull_manifest()
    assert stats == {"ok": False, "error": "network"}


# ── fetch_missing_media ──────────────────────────────────────────────────────


def test_fetch_missing_media_writes_blob(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    client = _make_client(tmp_path, session)
    client.cache.apply_rows(
        "lw_media", [{"id": 7, "file_path": "/srv/7.jpg", "updated_at": "t0"}]
    )

    session.get.return_value = FakeResponse(
        body_bytes=b"JPEGDATA",
        headers={"ETag": '"m7-t0"'},
    )
    stats = client.fetch_missing_media()
    assert stats["fetched"] == 1
    blob = client.cache.get_media_blob(7)
    assert blob is not None
    assert blob["etag"] == '"m7-t0"'
    assert Path(blob["file_path"]).read_bytes() == b"JPEGDATA"


def test_fetch_missing_media_skips_already_cached(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    client = _make_client(tmp_path, session)
    client.cache.apply_rows("lw_media", [{"id": 7, "updated_at": "t0"}])
    blob_path = client.media_dir / "7.bin"
    blob_path.write_bytes(b"already")
    client.cache.record_media_blob(7, str(blob_path), '"e"', "t0")

    stats = client.fetch_missing_media()
    assert stats == {"fetched": 0, "skipped": 1, "errored": 0}
    session.get.assert_not_called()


def test_fetch_missing_media_404_purges(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    client = _make_client(tmp_path, session)
    client.cache.apply_rows("lw_media", [{"id": 9, "updated_at": "t0"}])
    # Pretend a previous tick downloaded this, but the file's gone (manually
    # removed by an admin). The cache entry remains; the on-disk file does
    # not. The sync should re-fetch, hit 404, and purge.
    client.cache.record_media_blob(9, str(client.media_dir / "9.bin"), None, "t0")

    session.get.return_value = FakeResponse(status_code=404)
    stats = client.fetch_missing_media()
    assert stats["errored"] == 1
    assert client.cache.get_media_blob(9) is None


# ── flush_events ─────────────────────────────────────────────────────────────


def test_flush_events_empty(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    client = _make_client(tmp_path, session)
    assert client.flush_events() == {"sent": 0}
    session.post.assert_not_called()


def test_flush_events_sends_and_clears(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    client = _make_client(tmp_path, session)
    client.cache.queue_event("face_tag_submit", {"media_id": 1}, "t0")
    client.cache.queue_event("media_delete", {"media_id": 2}, "t1")

    session.post.return_value = FakeResponse(json_body={"ok": True, "ids": [1, 2]})
    stats = client.flush_events()
    assert stats == {"sent": 2}
    assert client.cache.pending_event_count() == 0
    _, kwargs = session.post.call_args
    assert kwargs["json"]["events"][0]["event_type"] == "face_tag_submit"


def test_flush_events_keeps_on_failure(tmp_path: Path):
    session = MagicMock()
    session.headers = {}
    client = _make_client(tmp_path, session)
    client.cache.queue_event("media_delete", {"media_id": 1}, "t0")
    session.post.return_value = FakeResponse(status_code=500)
    stats = client.flush_events()
    assert stats["sent"] == 0
    # Still pending for retry next tick.
    assert client.cache.pending_event_count() == 1


# ── fetch_background_video ────────────────────────────────────────────────────


def _client_with_slug(tmp_path: Path, session: MagicMock, slug: str = "jefferson") -> SyncClient:
    cache = KioskLocalCache(tmp_path / "cache.db")
    client = SyncClient(
        backend="https://backend.example",
        token="tok",
        cache=cache,
        media_dir=tmp_path / "media",
        session=session,
        slug=slug,
        video_dir=tmp_path / "video",
    )
    client.video_dir.mkdir(parents=True, exist_ok=True)
    return client


def test_fetch_background_video_downloads_all_parts(tmp_path: Path):
    session = MagicMock(); session.headers = {}
    client = _client_with_slug(tmp_path, session)
    api = FakeResponse(json_body={"background_video": {
        "video_url": "/jefferson/legacy-wall/background-video/file",
        "mp4_url": "/jefferson/legacy-wall/background-video/mp4",
        "poster_url": "/jefferson/legacy-wall/background-video/poster",
    }})
    session.get.side_effect = [
        api,
        FakeResponse(body_bytes=b"WEBM", headers={"ETag": '"w"'}),
        FakeResponse(body_bytes=b"MP4", headers={"ETag": '"m"'}),
        FakeResponse(body_bytes=b"JPG", headers={"ETag": '"p"'}),
    ]
    stats = client.fetch_background_video()
    assert stats["ok"] and stats["fetched"] == 3
    assert client.cache.get_video_blob("webm")["etag"] == '"w"'
    assert (client.video_dir / "webm.bin").read_bytes() == b"WEBM"
    assert (client.video_dir / "mp4.bin").read_bytes() == b"MP4"
    assert (client.video_dir / "poster.bin").read_bytes() == b"JPG"


def test_fetch_background_video_no_video_purges_cache(tmp_path: Path):
    session = MagicMock(); session.headers = {}
    client = _client_with_slug(tmp_path, session)
    p = client.video_dir / "webm.bin"; p.write_bytes(b"old")
    client.cache.record_video_blob("webm", str(p), '"w"', "t0")
    session.get.side_effect = [FakeResponse(json_body={"background_video": None})]
    stats = client.fetch_background_video()
    assert stats["ok"] and stats["purged"] == 1
    assert client.cache.get_video_blob("webm") is None
    assert not p.exists()


def test_fetch_background_video_304_keeps_cached(tmp_path: Path):
    session = MagicMock(); session.headers = {}
    client = _client_with_slug(tmp_path, session)
    p = client.video_dir / "webm.bin"; p.write_bytes(b"cached")
    client.cache.record_video_blob("webm", str(p), '"w"', "t0")
    session.get.side_effect = [
        FakeResponse(json_body={"background_video": {
            "video_url": "/jefferson/legacy-wall/background-video/file"}}),
        FakeResponse(status_code=304),
    ]
    stats = client.fetch_background_video()
    assert stats["skipped"] == 1 and stats["fetched"] == 0
    assert p.read_bytes() == b"cached"  # untouched


def test_fetch_background_video_no_slug_noops(tmp_path: Path):
    session = MagicMock(); session.headers = {}
    cache = KioskLocalCache(tmp_path / "cache.db")
    client = SyncClient(
        backend="https://b.example", token="t", cache=cache,
        media_dir=tmp_path / "media", session=session, slug="", video_dir=tmp_path / "video")
    stats = client.fetch_background_video()
    assert stats == {"ok": False, "error": "no_slug"}
    session.get.assert_not_called()
