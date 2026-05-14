"""Read-side accessors for the local Legacy Wall renderer.

The kiosk's sync engine writes rows into KioskLocalCache as JSON blobs. The
renderer needs to pull a slim, public-safe subset back out: media id, a
caption, the on-disk path of the downloaded blob, and basic ordering hints.

Filtering rules mirror the cloud renderer's "kiosk slideshow" view:
  - Drop rows whose `published` flag is explicitly false. Rows without that
    field are assumed visible (older cache snapshots predate the flag).
  - Drop rows with no on-disk blob — we can't show what we haven't
    downloaded yet. (sync_client downloads up to 50/tick, so a fresh kiosk
    will start blank and fill in over the next few minutes.)
  - Sort by media.taken_at desc, falling back to created_at.

Kept separate from the FastAPI route to keep the route file thin and so
the read logic is unit-testable without an HTTP client.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .local_cache import KioskLocalCache


def _row_sort_key(row: Dict[str, Any]) -> Tuple[str, int]:
    """Sort key — newest first. Falls back to created_at, then row_id."""
    taken = row.get("taken_at") or row.get("created_at") or ""
    return (taken, int(row.get("id") or 0))


def collect_slideshow_media(cache: KioskLocalCache) -> List[Dict[str, Any]]:
    """Return the list of media rows ready for the slideshow.

    Each entry contains everything the renderer JS needs: `id`, `caption`,
    `alt_text`, `taken_at`. The on-disk path is intentionally NOT exposed
    — the renderer fetches `/legacy-wall/media/{id}` and the on-device
    server resolves the file path internally.
    """
    rows = cache.list_rows("lw_media")
    out: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("published") is False:
            continue
        media_id = row.get("id")
        if media_id is None:
            continue
        blob = cache.get_media_blob(int(media_id))
        if blob is None or not blob.get("file_path"):
            continue
        out.append(
            {
                "id": int(media_id),
                "caption": row.get("caption") or "",
                "alt_text": row.get("alt_text") or "",
                "taken_at": row.get("taken_at") or "",
            }
        )
    out.sort(key=lambda r: _row_sort_key({"id": r["id"], "taken_at": r["taken_at"]}), reverse=True)
    return out


def resolve_media_file_path(
    cache: KioskLocalCache, media_id: int
) -> Optional[str]:
    """Return the on-disk path of a media blob, or None if the kiosk hasn't
    downloaded it (or the media row was tombstoned). Used by the blob route.
    """
    blob = cache.get_media_blob(int(media_id))
    if blob is None:
        return None
    file_path = blob.get("file_path")
    if not file_path:
        return None
    return file_path
