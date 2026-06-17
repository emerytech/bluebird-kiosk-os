"""Sync loop client — pulls the manifest, applies rows, fetches media,
and drains the outbound event queue. Runs as a long-lived service via
`python3 -m bluebird_kiosk.services.sync_client`.

Three logical phases per tick:

  1. PULL: GET /api/public/kiosk/sync/manifest?since=<cursor>
       Apply rows to KioskLocalCache. Tombstones (deleted_at set) cause
       the corresponding row to be removed locally; if the deleted row is
       a media row, the on-disk blob is also unlinked.
  2. FETCH: For every lw_media row in the cache whose file isn't in the
       media_blobs table, GET /api/public/kiosk/sync/media/{id} and write
       to /var/lib/bluebird-kiosk/media/<media_id>.bin. Skip on 304.
  3. PUSH: POST any pending_events to /api/public/kiosk/sync/events.
       Delete from queue on success.

Failure modes are intentionally soft: any network error → log + retry on
the next tick. The cursor only advances when the manifest call returns
200, so a half-applied tick is safe.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .. import __version__, config
from .local_cache import KioskLocalCache


logger = logging.getLogger("bluebird-kiosk.sync")


# ── Paths ────────────────────────────────────────────────────────────────────

LICENSE_TOKEN_PATH = Path(
    os.environ.get("BLUEBIRD_LICENSE_TOKEN", "/etc/bluebird/license.token")
)
LOCAL_CACHE_PATH = Path(
    os.environ.get("BLUEBIRD_LOCAL_CACHE_DB", "/var/lib/bluebird-kiosk/cache.db")
)
MEDIA_DIR = Path(
    os.environ.get("BLUEBIRD_LOCAL_MEDIA_DIR", "/var/lib/bluebird-kiosk/media")
)
VIDEO_DIR = Path(
    os.environ.get("BLUEBIRD_LOCAL_VIDEO_DIR", "/var/lib/bluebird-kiosk/video")
)

# (kiosk URL key in the backgrounds API, local cache 'kind', filename, mime)
_BG_VIDEO_PARTS = (
    ("video_url", "webm", "webm.bin"),
    ("mp4_url", "mp4", "mp4.bin"),
    ("poster_url", "poster", "poster.bin"),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_license_token() -> Optional[str]:
    if not LICENSE_TOKEN_PATH.exists():
        return None
    try:
        return LICENSE_TOKEN_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# ── Single-tick sync logic ───────────────────────────────────────────────────


class SyncClient:
    """Stateful sync client. Holds the cache + HTTP session + config so
    individual methods stay easy to unit-test."""

    def __init__(
        self,
        backend: str,
        token: str,
        cache: KioskLocalCache,
        media_dir: Path,
        session: Optional[requests.Session] = None,
        request_timeout: float = 30.0,
        slug: str = "",
        video_dir: Optional[Path] = None,
    ) -> None:
        self.backend = backend.rstrip("/")
        self.token = token
        self.slug = (slug or "").strip("/")
        self.cache = cache
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        # video_dir is created lazily (first download) — its default is a system
        # path that may not be writable in tests / before first use.
        self.video_dir = Path(video_dir) if video_dir else VIDEO_DIR
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": f"BlueBirdKiosk/{__version__}",
            }
        )
        self.request_timeout = request_timeout

    # ── Phase 1: PULL ────────────────────────────────────────────────────────

    def pull_manifest(self) -> Dict[str, Any]:
        """Pull the manifest and apply its rows to the local cache.

        Returns a stats dict: {ok, new_cursor, rows_upserted, rows_deleted}.
        On any HTTP / network failure returns {ok: False, error: <msg>}.
        """
        since = self.cache.get_cursor() or ""
        url = f"{self.backend}/api/public/kiosk/sync/manifest"
        try:
            resp = self.session.get(
                url, params={"since": since}, timeout=self.request_timeout
            )
        except requests.RequestException as exc:
            logger.warning("manifest: network error: %s", exc)
            return {"ok": False, "error": "network"}

        if resp.status_code == 401:
            logger.error("manifest: 401 — license token rejected. Halting tick.")
            return {"ok": False, "error": "unauthorized"}
        if resp.status_code >= 400:
            logger.warning(
                "manifest: backend rejected status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return {"ok": False, "error": f"http_{resp.status_code}"}

        try:
            data = resp.json()
        except ValueError:
            logger.warning("manifest: non-json body")
            return {"ok": False, "error": "non_json"}

        rows_by_table: Dict[str, List[Dict[str, Any]]] = data.get("rows") or {}
        new_cursor: str = data.get("cursor_at") or ""

        total_up = 0
        total_del = 0
        for table_name, rows in rows_by_table.items():
            up, dl = self.cache.apply_rows(table_name, rows)
            total_up += up
            total_del += dl
            # If a media row was tombstoned, drop the on-disk blob.
            if table_name == "lw_media" and dl > 0:
                for row in rows:
                    if row.get("deleted_at") and row.get("id") is not None:
                        self._purge_media_file(int(row["id"]))

        if new_cursor:
            self.cache.set_cursor(new_cursor)
        return {
            "ok": True,
            "new_cursor": new_cursor,
            "rows_upserted": total_up,
            "rows_deleted": total_del,
        }

    # ── Phase 2: FETCH ───────────────────────────────────────────────────────

    def fetch_missing_media(self, limit: int = 50) -> Dict[str, Any]:
        """Download blobs for any lw_media row not yet on disk. Caps per-tick
        so a fresh kiosk doesn't try to download everything in one burst.
        Re-runs every tick will catch up over time.
        """
        rows = self.cache.list_rows("lw_media")
        fetched = 0
        skipped = 0
        errored = 0
        for media in rows:
            if fetched >= limit:
                break
            media_id = media.get("id")
            if media_id is None:
                continue
            existing = self.cache.get_media_blob(int(media_id))
            if existing:
                cached_path = Path(existing["file_path"])
                # Field-confirmed 2026-06-05: it's possible for the
                # previous tick to have left a 0-byte file (transient
                # cloud error, network blip mid-stream, or a server
                # response with empty body that we wrote as-is). The
                # original check (.is_file()) skipped those forever,
                # leaving the kiosk with empty placeholders that
                # render as broken images in the dashboard. Treat any
                # zero-byte file as a cache miss so it gets re-fetched.
                if cached_path.is_file() and cached_path.stat().st_size > 0:
                    # Already cached. Could revalidate via ETag but we
                    # trust the cursor — if the media row's updated_at
                    # changed, the manifest would have given us a new
                    # row in this same tick and we'd re-fetch below.
                    skipped += 1
                    continue
            ok = self._download_media(int(media_id), existing)
            if ok:
                fetched += 1
            else:
                errored += 1
        return {"fetched": fetched, "skipped": skipped, "errored": errored}

    def _download_media(
        self, media_id: int, existing: Optional[Dict[str, Any]]
    ) -> bool:
        url = f"{self.backend}/api/public/kiosk/sync/media/{media_id}"
        headers: Dict[str, str] = {}
        if existing and existing.get("etag"):
            headers["If-None-Match"] = existing["etag"]
        try:
            resp = self.session.get(
                url, headers=headers, timeout=self.request_timeout, stream=True
            )
        except requests.RequestException as exc:
            logger.warning("media %s: network error: %s", media_id, exc)
            return False
        if resp.status_code == 304:
            # Server agrees nothing changed; treat as success without touching
            # the file.
            return True
        if resp.status_code == 404:
            # Backend says the file is gone — local should reflect that.
            self._purge_media_file(media_id)
            return False
        if resp.status_code >= 400:
            logger.warning(
                "media %s: backend rejected status=%s", media_id, resp.status_code
            )
            return False

        target = self.media_dir / f"{media_id}.bin"
        try:
            with open(target, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
        except OSError as exc:
            logger.warning("media %s: write failed: %s", media_id, exc)
            return False

        # Verify the file actually has content. We've seen the response
        # iter_content() loop yield zero chunks for some IDs even on a
        # 200 status (likely transient backend/CDN issues — the cloud
        # had the bytes on retry). Without this check, the previous
        # version recorded a media_blobs row for a 0-byte file and
        # subsequently skipped that media forever. Field-confirmed
        # 2026-06-05: 20 of NEN's 274 photos were 0 bytes for this
        # reason.
        try:
            actual_size = target.stat().st_size
        except OSError:
            actual_size = 0
        if actual_size == 0:
            logger.warning(
                "media %s: downloaded 0 bytes (status=%s, content-length=%s); "
                "leaving as cache miss to retry next tick",
                media_id, resp.status_code, resp.headers.get("Content-Length"),
            )
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return False

        self.cache.record_media_blob(
            media_id=media_id,
            file_path=str(target),
            etag=resp.headers.get("ETag"),
            fetched_at=_now_iso(),
        )
        return True

    def _purge_media_file(self, media_id: int) -> None:
        file_path = self.cache.delete_media_blob(media_id)
        if file_path:
            try:
                Path(file_path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("media %s: unlink failed: %s", media_id, exc)

    # ── Phase 2b: FETCH background video ─────────────────────────────────────

    def fetch_background_video(self) -> Dict[str, Any]:
        """Download the tenant's Legacy Wall videos into the local cache so the
        wall plays them from the local cache server instead of re-streaming the
        backend on every load / reboot / rotation cycle (KI-033).

        Two sets: the single DESIGNATED wall background (bare URL → video_blobs)
        and the background-ROTATION / on-demand aerial clips (?video_id=N →
        rotation_video_blobs). Both are ETag-revalidated against the PUBLIC serve
        endpoints (Starlette FileResponse sets an ETag), so an unchanged video is
        a cheap 304 each tick. Soft-fails on any error. De-selected videos (and a
        removed designated video) have their cached files purged.
        """
        if not self.slug:
            return {"ok": False, "error": "no_slug"}
        api_url = f"{self.backend}/{self.slug}/legacy-wall/api/backgrounds"
        try:
            resp = self.session.get(api_url, timeout=self.request_timeout)
        except requests.RequestException as exc:
            logger.warning("bg-video: backgrounds api network error: %s", exc)
            return {"ok": False, "error": "network"}
        if resp.status_code >= 400:
            return {"ok": False, "error": f"http_{resp.status_code}"}
        try:
            data = resp.json() or {}
        except ValueError:
            return {"ok": False, "error": "non_json"}
        bgv = data.get("background_video")
        # ── Designated wall background (single, bare URL) ──
        if not bgv:
            # No enabled designated video — drop anything we cached previously.
            result: Dict[str, Any] = {"ok": True, "purged": self._purge_background_video()}
        else:
            fetched = skipped = errored = 0
            for url_key, kind, fname in _BG_VIDEO_PARTS:
                rel = bgv.get(url_key)
                if not rel:
                    # e.g. mp4 absent on a not-yet-dual-encoded row — drop stale.
                    self._purge_video_part(kind)
                    continue
                full_url = f"{self.backend}{rel}" if rel.startswith("/") else rel
                outcome = self._download_video_part(kind, fname, full_url)
                if outcome == "fetched":
                    fetched += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    errored += 1
            result = {"ok": True, "fetched": fetched, "skipped": skipped, "errored": errored}
        # ── Background-rotation / on-demand aerial clips (?video_id=N) ──
        result["rotation"] = self._sync_rotation_videos(
            data.get("slideshow_videos") or []
        )
        return result

    def _fetch_blob_to_file(
        self, url: str, etag: Optional[str], fname: str, label: str
    ) -> tuple:
        """Shared GET→file for a video blob. Returns (outcome, new_etag) where
        outcome is 'fetched' | 'skipped' | 'notfound' | 'errored'. On 'fetched'
        a non-empty file is written to self.video_dir/fname and new_etag is the
        response ETag; 'notfound' (404) signals the caller to purge. Never
        raises — every failure is logged and soft-returned."""
        headers: Dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        try:
            resp = self.session.get(
                url, headers=headers, timeout=self.request_timeout, stream=True
            )
        except requests.RequestException as exc:
            logger.warning("%s: network error: %s", label, exc)
            return ("errored", None)
        if resp.status_code == 304:
            return ("skipped", None)
        if resp.status_code == 404:
            return ("notfound", None)
        if resp.status_code >= 400:
            logger.warning("%s: backend status=%s", label, resp.status_code)
            return ("errored", None)
        target = self.video_dir / fname
        try:
            self.video_dir.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
        except OSError as exc:
            logger.warning("%s: write failed: %s", label, exc)
            return ("errored", None)
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return ("errored", None)
        return ("fetched", resp.headers.get("ETag"))

    def _download_video_part(self, kind: str, fname: str, url: str) -> str:
        existing = self.cache.get_video_blob(kind)
        outcome, new_etag = self._fetch_blob_to_file(
            url, (existing or {}).get("etag"), fname, "bg-video %s" % kind
        )
        if outcome == "notfound":
            self._purge_video_part(kind)
            return "errored"
        if outcome == "fetched":
            self.cache.record_video_blob(
                kind=kind,
                file_path=str(self.video_dir / fname),
                etag=new_etag,
                fetched_at=_now_iso(),
            )
        return outcome if outcome in ("fetched", "skipped") else "errored"

    def _purge_video_part(self, kind: str) -> None:
        fp = self.cache.delete_video_blob(kind)
        if fp:
            try:
                Path(fp).unlink(missing_ok=True)
            except OSError:
                pass

    def _purge_background_video(self) -> int:
        purged = 0
        for blob in self.cache.list_video_blobs():
            self._purge_video_part(blob["kind"])
            purged += 1
        return purged

    def _sync_rotation_videos(self, slideshow_videos: List[Any]) -> Dict[str, Any]:
        """Mirror each background-rotation video (addressed by ?video_id=N) into
        the local cache, and purge any cached rotation video no longer selected.
        A designated video that is ALSO in the rotation appears here with a BARE
        url (no video_id=) — it is already cached as the single background, so we
        skip it. Makes NO network calls when there are no per-id rotation videos
        (so a tenant without rotation costs only one local DB read)."""
        keep_ids = set()
        fetched = skipped = errored = 0
        for entry in slideshow_videos:
            if not isinstance(entry, dict):
                continue
            vid = entry.get("id")
            # Only per-id entries are ours; the designated (bare-url) video is
            # served from the single-background cache, not by video_id.
            if vid is None or "video_id=" not in (entry.get("video_url") or ""):
                continue
            try:
                vid = int(vid)
            except (TypeError, ValueError):
                continue
            keep_ids.add(vid)
            for url_key, kind, fname in _BG_VIDEO_PARTS:
                rel = entry.get(url_key)
                if not rel:
                    self._purge_rotation_part(vid, kind)
                    continue
                full_url = f"{self.backend}{rel}" if rel.startswith("/") else rel
                outcome = self._download_rotation_part(
                    vid, kind, f"rot-{vid}-{fname}", full_url
                )
                if outcome == "fetched":
                    fetched += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    errored += 1
        purged = self._purge_rotation_videos_except(keep_ids)
        return {
            "fetched": fetched,
            "skipped": skipped,
            "errored": errored,
            "purged": purged,
        }

    def _download_rotation_part(
        self, video_id: int, kind: str, fname: str, url: str
    ) -> str:
        existing = self.cache.get_rotation_video_blob(video_id, kind)
        outcome, new_etag = self._fetch_blob_to_file(
            url,
            (existing or {}).get("etag"),
            fname,
            "rot-video %s/%s" % (video_id, kind),
        )
        if outcome == "notfound":
            self._purge_rotation_part(video_id, kind)
            return "errored"
        if outcome == "fetched":
            self.cache.record_rotation_video_blob(
                video_id=video_id,
                kind=kind,
                file_path=str(self.video_dir / fname),
                etag=new_etag,
                fetched_at=_now_iso(),
            )
        return outcome if outcome in ("fetched", "skipped") else "errored"

    def _purge_rotation_part(self, video_id: int, kind: str) -> None:
        fp = self.cache.delete_rotation_video_blob(video_id, kind)
        if fp:
            try:
                Path(fp).unlink(missing_ok=True)
            except OSError:
                pass

    def _purge_rotation_videos_except(self, keep_ids: set) -> int:
        """Drop cached rotation videos whose id is no longer selected. Returns
        the number of distinct videos purged."""
        stale_ids = {
            blob["video_id"]
            for blob in self.cache.list_rotation_video_blobs()
            if blob["video_id"] not in keep_ids
        }
        for vid in stale_ids:
            for fp in self.cache.delete_rotation_video(vid):
                try:
                    Path(fp).unlink(missing_ok=True)
                except OSError:
                    pass
        return len(stale_ids)

    # ── Phase 3: PUSH ────────────────────────────────────────────────────────

    def flush_events(self, batch_size: int = 50) -> Dict[str, Any]:
        events = self.cache.list_pending_events(limit=batch_size)
        if not events:
            return {"sent": 0}
        payload = {
            "events": [
                {"event_type": ev["event_type"], "event_data": ev["event_data"]}
                for ev in events
            ]
        }
        url = f"{self.backend}/api/public/kiosk/sync/events"
        try:
            resp = self.session.post(
                url, json=payload, timeout=self.request_timeout
            )
        except requests.RequestException as exc:
            logger.warning("events: network error: %s", exc)
            return {"sent": 0, "error": "network"}
        if resp.status_code >= 400:
            logger.warning(
                "events: backend rejected status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return {"sent": 0, "error": f"http_{resp.status_code}"}
        # Successfully accepted by server; drop them from the local queue.
        self.cache.delete_pending_events([ev["id"] for ev in events])
        return {"sent": len(events)}

    # ── Full tick ────────────────────────────────────────────────────────────

    def run_once(self) -> Dict[str, Any]:
        manifest_stats = self.pull_manifest()
        if not manifest_stats.get("ok"):
            # If pull failed we still try to flush events + refresh diagnostics
            # — they don't depend on cursor state and may have been queued offline.
            push_stats = self.flush_events()
            diag_stats = self.refresh_diagnostics()
            return {
                "manifest": manifest_stats,
                "push": push_stats,
                "diagnostics": diag_stats,
            }
        fetch_stats = self.fetch_missing_media()
        video_stats = self.fetch_background_video()
        push_stats = self.flush_events()
        diag_stats = self.refresh_diagnostics()
        return {
            "manifest": manifest_stats,
            "fetch": fetch_stats,
            "video": video_stats,
            "push": push_stats,
            "diagnostics": diag_stats,
        }

    def refresh_diagnostics(self) -> Dict[str, Any]:
        """Pull the super-admin diagnostic allow-list and cache it locally.
        Soft-failure on any error — the on-disk cache stays usable offline."""
        from . import diagnostics
        result = diagnostics.refresh_from_server(self.backend, self.token)
        if result is None:
            return {"ok": False}
        return {"ok": True, "count": len(result)}


# ── Service entry point ──────────────────────────────────────────────────────


def _build_client() -> Optional[SyncClient]:
    cfg = config.read_config()
    backend = cfg.get("BLUEBIRD_BACKEND") or ""
    slug = cfg.get("SCHOOL_SLUG") or ""
    token = read_license_token()
    if not backend:
        logger.info("sync: BLUEBIRD_BACKEND not set — sleeping")
        return None
    if not token:
        logger.info("sync: no license.token yet — sleeping")
        return None
    cache = KioskLocalCache(LOCAL_CACHE_PATH)
    return SyncClient(
        backend=backend, token=token, cache=cache, media_dir=MEDIA_DIR,
        slug=slug, video_dir=VIDEO_DIR,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = config.read_config()
    try:
        interval = max(15, int(cfg.get("SYNC_INTERVAL_SEC") or 60))
    except ValueError:
        interval = 60
    logger.info("sync: starting (interval=%ss)", interval)
    while True:
        client = _build_client()
        if client is not None:
            try:
                stats = client.run_once()
                logger.info("sync: tick %s", stats)
            except Exception as exc:  # noqa: BLE001
                logger.exception("sync: unexpected error: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main() or 0)
