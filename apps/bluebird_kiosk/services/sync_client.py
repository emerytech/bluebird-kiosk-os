"""Sync loop client — pulls the manifest, applies rows, fetches media,
and drains the outbound event queue. Runs as a long-lived service via
`python3 -m bluebird_kiosk.services.sync_client`.

Three logical phases per tick:

  1. PULL: GET /api/public/kiosk/sync/manifest?since=<cursor>
       Apply validated rows and authoritative snapshot inventories. Tombstones (deleted_at set) cause
       the corresponding row to be removed locally; if the deleted row is
       a media row, the on-disk blob is also unlinked.
  2. FETCH: For every lw_media row in the cache whose file isn't in the
       media_blobs table or with invalid/version-stale bytes, fetch unconditionally
       and atomically replace /var/lib/bluebird-kiosk/media/<media_id>.bin.
  3. PUSH: POST any pending_events to /api/public/kiosk/sync/events.
       Delete from queue on success.

Failure modes are intentionally soft: any network error → log + retry on
the next tick. The cursor only advances when the manifest call returns
200, so a half-applied tick is safe.
"""
from __future__ import annotations

import hashlib
import tempfile
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .. import __version__, config
from .local_cache import KioskLocalCache, media_revision


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
        self._purge_orphan_media()

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

        if not isinstance(data, dict):
            return {"ok": False, "error": "invalid_manifest"}
        rows_by_table = data.get("rows")
        snapshots = data.get("snapshot_tables", [])
        # Validate the entire response before any destructive reconciliation.
        if (not isinstance(rows_by_table, dict) or not isinstance(snapshots, list)
                or any(not isinstance(t, str) or not t.startswith("lw_") for t in rows_by_table)
                or any(not isinstance(t, str) or t not in rows_by_table for t in snapshots)):
            return {"ok": False, "error": "invalid_manifest"}
        for rows in rows_by_table.values():
            if (not isinstance(rows, list) or any(not isinstance(row, dict) or
                    type(row.get("id")) is not int or not 0 < row["id"] < 2**63 for row in rows)
                    or len({row["id"] for row in rows}) != len(rows)):
                return {"ok": False, "error": "invalid_manifest"}
        new_cursor = data.get("cursor_at") or ""
        if not isinstance(new_cursor, str):
            return {"ok": False, "error": "invalid_manifest"}
        total_up = 0
        total_del = 0
        for table_name, rows in rows_by_table.items():
            if table_name == "lw_media":
                incoming = {row["id"]: row for row in rows}
                for old in self.cache.list_rows("lw_media"):
                    new = incoming.get(old["id"])
                    if ((new is None and table_name in snapshots) or
                            (new is not None and (new.get("deleted_at") or new.get("published") in (False, 0)
                             or media_revision(old) != media_revision(new)))):
                        self._purge_media_file(old["id"])
                # Tombstones also remove a stray blob even if its mirror row was lost.
                for row in rows:
                    if row.get("deleted_at"):
                        self._purge_media_file(row["id"])
        total_up, total_del = self.cache.apply_manifest(rows_by_table, snapshots, new_cursor)
        self._purge_orphan_media()

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
            if media.get("published") in (False, 0):
                self._purge_media_file(int(media_id))
                continue
            existing = self.cache.get_media_blob(int(media_id))
            if self.cache.valid_media_blob(int(media_id)):
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
        # A missing/invalid/version-stale blob must be fetched unconditionally.
        # An ETag describes server bytes; it is not proof that we still have them.
        row = self.cache.get_row("lw_media", media_id) or {}
        revision = media_revision(row)
        target = self.media_dir / f"{media_id}.bin"
        temporary = None
        resp = None
        try:
            for attempt in range(2):
                resp = self.session.get(url, headers={"Accept-Encoding": "identity"},
                                        timeout=self.request_timeout, stream=True)
                if resp.status_code != 304:
                    break
                if hasattr(resp, "close"):
                    resp.close()
            if resp.status_code == 304:
                return False
            if resp.status_code == 404:
                self._purge_media_file(media_id)
                return False
            if resp.status_code != 200:
                return False
            if resp.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                return False
            content_type = resp.headers.get("Content-Type", "").split(";")[0].lower()
            if content_type and not (content_type.startswith("image/") or content_type == "application/octet-stream"):
                return False
            server_version = resp.headers.get("X-Media-Version")
            if row.get("media_version") and server_version and server_version != revision:
                return False
            declared = resp.headers.get("Content-Length")
            expected = int(declared) if declared is not None else None
            if expected is None and not resp.headers.get("X-Content-SHA256"):
                return False
            maximum = 64 * 1024 * 1024
            if expected is not None and not 0 < expected <= maximum:
                return False
            fd, temporary = tempfile.mkstemp(prefix=f".{media_id}.", suffix=".part", dir=self.media_dir)
            size = 0
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        size += len(chunk)
                        if size > maximum or (expected is not None and size > expected):
                            raise ValueError("Oversize media response")
                        digest.update(chunk)
                        fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            if size == 0 or (expected is not None and size != expected):
                raise ValueError("Incomplete media response")
            expected_hash = resp.headers.get("X-Content-SHA256")
            if expected_hash and digest.hexdigest() != expected_hash:
                raise ValueError("Media checksum mismatch")
            current = self.cache.get_row("lw_media", media_id)
            if not current or current.get("published") in (False, 0) or media_revision(current) != revision:
                return False
            os.replace(temporary, target)
            temporary = None
            return self.cache.record_media_blob(media_id, str(target), resp.headers.get("ETag"),
                                                _now_iso(), source_version=revision)
        except (OSError, ValueError, requests.RequestException) as exc:
            logger.warning("media %s: download incomplete: %s", media_id, type(exc).__name__)
            return False
        finally:
            if resp is not None and hasattr(resp, "close"):
                resp.close()
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def _purge_media_file(self, media_id: int) -> None:
        # Never trust a stored absolute path as deletion authority.
        self.cache.delete_media_blob(media_id)
        try:
            (self.media_dir / f"{int(media_id)}.bin").unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("media %s: unlink failed: %s", media_id, exc)

    def _purge_orphan_media(self):
        # Retry unlink after a crash/failure between DB reconciliation and file IO.
        keep = {int(r["id"]) for r in self.cache.list_rows("lw_media")
                if not r.get("deleted_at") and r.get("published") not in (False, 0)}
        for blob in self.cache.list_media_blobs():
            if blob["media_id"] not in keep:
                self._purge_media_file(blob["media_id"])
        for path in self.media_dir.glob("*.bin"):
            if path.stem.isdigit() and int(path.stem) not in keep:
                self._purge_media_file(int(path.stem))

        # Only our stale atomic-download staging names; never source/media files.
        for path in self.media_dir.glob(".*.part"):
            parts = path.name.split(".")
            try:
                if len(parts) == 4 and parts[1].isdigit() and time.time() - path.lstat().st_mtime > 86400:
                    path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove stale photo staging file")

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
