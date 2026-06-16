"""KioskLocalCache — on-device SQLite mirror of synced Legacy Wall tables.

The Phase 1 backend manifest returns rows from 13 LW tables (collections,
albums, media, …). Rather than recreating each table's exact schema on the
kiosk, we store rows as JSON blobs in a single generic table keyed by
(table_name, row_id). This lets the server evolve column shapes without
needing a coordinated kiosk-OS release.

  lw_mirror
    table_name TEXT, row_id INTEGER, cursor_at TEXT, deleted_at TEXT,
    payload TEXT (json), PRIMARY KEY(table_name, row_id)

  sync_state
    key TEXT PRIMARY KEY, value TEXT  -- single-row k/v: 'cursor_at', etc.

  pending_events
    id, event_type, event_data (json), queued_at
    Outbound queue for face_tag_submit / media_metadata_edit / media_delete.
    Drained by the sync loop POSTing to /api/public/kiosk/sync/events.

  media_blobs
    media_id INTEGER PRIMARY KEY, etag TEXT, file_path TEXT, fetched_at TEXT
    Tracks which media files have been downloaded to the on-disk cache and
    their ETag so we can skip re-downloads with If-None-Match.

The cache lives in /var/lib/bluebird-kiosk/cache.db (path overridable via
the constructor for tests).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lw_mirror (
    table_name TEXT    NOT NULL,
    row_id     INTEGER NOT NULL,
    cursor_at  TEXT    NOT NULL,
    deleted_at TEXT    NULL,
    payload    TEXT    NOT NULL,
    PRIMARY KEY (table_name, row_id)
);
CREATE INDEX IF NOT EXISTS idx_lw_mirror_table_cursor
    ON lw_mirror(table_name, cursor_at);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT    NOT NULL,
    event_data  TEXT    NOT NULL,
    queued_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS media_blobs (
    media_id    INTEGER PRIMARY KEY,
    etag        TEXT    NULL,
    file_path   TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL
);

-- The Legacy Wall background VIDEO (one per tenant/kiosk). Keyed by 'kind'
-- ('webm' | 'mp4' | 'poster') since there is only ever one video per kiosk.
-- Downloaded proactively by sync_client (ETag-revalidated) and served from the
-- local cache server so the wall plays the aerial loop without re-streaming
-- from the backend on every page load / reboot.
CREATE TABLE IF NOT EXISTS video_blobs (
    kind        TEXT    PRIMARY KEY,
    etag        TEXT    NULL,
    file_path   TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL
);
"""


class KioskLocalCache:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.executescript(_SCHEMA_SQL)

    # ── Cursor / sync state ──────────────────────────────────────────────────

    def get_cursor(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = ?", ("cursor_at",)
            ).fetchone()
        return row["value"] if row else None

    def set_cursor(self, cursor_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("cursor_at", cursor_at),
            )

    def get_state(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ── Row mirror ───────────────────────────────────────────────────────────

    def apply_rows(
        self, table_name: str, rows: Iterable[Dict[str, Any]]
    ) -> Tuple[int, int]:
        """Upsert the given rows into the mirror. Rows with a non-null
        deleted_at are removed from the mirror entirely (tombstones).

        Returns (upserted_count, deleted_count).

        Each row dict must have an 'id' field. The cursor column varies
        per table — we accept whichever of updated_at / created_at is
        present; if neither exists we fall back to '' (forces re-pull next
        time which is harmless).
        """
        upserted = 0
        deleted = 0
        with self._connect() as conn:
            for row in rows:
                row_id = row.get("id")
                if row_id is None:
                    continue
                deleted_at = row.get("deleted_at")
                if deleted_at:
                    cur = conn.execute(
                        "DELETE FROM lw_mirror "
                        "WHERE table_name = ? AND row_id = ?",
                        (table_name, int(row_id)),
                    )
                    if cur.rowcount:
                        deleted += 1
                    continue
                cursor_at = (
                    row.get("updated_at")
                    or row.get("created_at")
                    or ""
                )
                conn.execute(
                    "INSERT INTO lw_mirror "
                    "(table_name, row_id, cursor_at, deleted_at, payload) "
                    "VALUES (?, ?, ?, NULL, ?) "
                    "ON CONFLICT(table_name, row_id) DO UPDATE SET "
                    "    cursor_at = excluded.cursor_at, "
                    "    deleted_at = NULL, "
                    "    payload = excluded.payload",
                    (
                        table_name,
                        int(row_id),
                        str(cursor_at),
                        json.dumps(row, separators=(",", ":")),
                    ),
                )
                upserted += 1
        return upserted, deleted

    def list_rows(self, table_name: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM lw_mirror WHERE table_name = ? "
                "ORDER BY row_id",
                (table_name,),
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def get_row(self, table_name: str, row_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM lw_mirror "
                "WHERE table_name = ? AND row_id = ?",
                (table_name, int(row_id)),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def row_count(self, table_name: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM lw_mirror WHERE table_name = ?",
                (table_name,),
            ).fetchone()
        return int(row["c"]) if row else 0

    # ── Media blob tracking ──────────────────────────────────────────────────

    def get_media_blob(self, media_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM media_blobs WHERE media_id = ?",
                (int(media_id),),
            ).fetchone()
        return dict(row) if row else None

    def record_media_blob(
        self,
        media_id: int,
        file_path: str,
        etag: Optional[str],
        fetched_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO media_blobs (media_id, etag, file_path, fetched_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(media_id) DO UPDATE SET "
                "    etag = excluded.etag, "
                "    file_path = excluded.file_path, "
                "    fetched_at = excluded.fetched_at",
                (int(media_id), etag, file_path, fetched_at),
            )

    def delete_media_blob(self, media_id: int) -> Optional[str]:
        """Remove the row, returning the on-disk file_path so the caller
        can unlink it. (We keep the file IO out of the cache module.)"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT file_path FROM media_blobs WHERE media_id = ?",
                (int(media_id),),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM media_blobs WHERE media_id = ?", (int(media_id),)
            )
        return row["file_path"]

    # ── Background-video blob tracking ───────────────────────────────────────

    def get_video_blob(self, kind: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM video_blobs WHERE kind = ?", (kind,)
            ).fetchone()
        return dict(row) if row else None

    def list_video_blobs(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM video_blobs").fetchall()
        return [dict(r) for r in rows]

    def record_video_blob(
        self, kind: str, file_path: str, etag: Optional[str], fetched_at: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO video_blobs (kind, etag, file_path, fetched_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET "
                "    etag = excluded.etag, "
                "    file_path = excluded.file_path, "
                "    fetched_at = excluded.fetched_at",
                (kind, etag, file_path, fetched_at),
            )

    def delete_video_blob(self, kind: str) -> Optional[str]:
        """Remove the row, returning the on-disk file_path so the caller can
        unlink it."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT file_path FROM video_blobs WHERE kind = ?", (kind,)
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM video_blobs WHERE kind = ?", (kind,))
        return row["file_path"]

    # ── Outbound event queue ─────────────────────────────────────────────────

    def queue_event(
        self, event_type: str, event_data: Dict[str, Any], queued_at: str
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO pending_events (event_type, event_data, queued_at) "
                "VALUES (?, ?, ?)",
                (
                    event_type,
                    json.dumps(event_data, separators=(",", ":")),
                    queued_at,
                ),
            )
            return int(cur.lastrowid)

    def list_pending_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, event_type, event_data, queued_at FROM pending_events "
                "ORDER BY id LIMIT ?",
                (int(limit),),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                data = json.loads(r["event_data"])
            except (TypeError, ValueError):
                data = {}
            out.append(
                {
                    "id": int(r["id"]),
                    "event_type": r["event_type"],
                    "event_data": data,
                    "queued_at": r["queued_at"],
                }
            )
        return out

    def delete_pending_events(self, event_ids: Iterable[int]) -> int:
        ids = [int(i) for i in event_ids]
        if not ids:
            return 0
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"DELETE FROM pending_events WHERE id IN ({placeholders})",
                ids,
            )
            return int(cur.rowcount or 0)

    def pending_event_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM pending_events"
            ).fetchone()
        return int(row["c"]) if row else 0
