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
    ) -> None:
        self.backend = backend.rstrip("/")
        self.token = token
        self.cache = cache
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
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
            if existing and Path(existing["file_path"]).is_file():
                # Already cached. Could revalidate via ETag but we trust
                # the cursor — if the media row's updated_at changed, the
                # manifest would have given us a new row in this same tick
                # and we'd re-fetch below.
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
        push_stats = self.flush_events()
        diag_stats = self.refresh_diagnostics()
        return {
            "manifest": manifest_stats,
            "fetch": fetch_stats,
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
    token = read_license_token()
    if not backend:
        logger.info("sync: BLUEBIRD_BACKEND not set — sleeping")
        return None
    if not token:
        logger.info("sync: no license.token yet — sleeping")
        return None
    cache = KioskLocalCache(LOCAL_CACHE_PATH)
    return SyncClient(
        backend=backend, token=token, cache=cache, media_dir=MEDIA_DIR
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
