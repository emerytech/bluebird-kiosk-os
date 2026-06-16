"""Background poller that mirrors THIS kiosk's active-incident state.

Polls the cloud `GET <backend>/api/public/kiosk/active-incident` (authenticated
with the device license bearer token) every few seconds and caches the latest
payload in memory. The local Legacy Wall page reads the cached state from the
loopback route `GET /legacy-wall/api/active-incident` — so the page never holds
the license token and never talks to the cloud directly.

Tenant isolation is the backend's responsibility: the bearer token resolves
server-side to exactly one tenant, and the endpoint lives under `/api/public/`
so the dashed-host 308 redirect (which would strip the Authorization header on a
cross-host hop to the canonical domain) never fires.

Fails closed: any error (no token, network, non-200, bad JSON) resolves the
cached state to {"active": false} — after a short grace if we were recently
active, so a transient blip doesn't yank a live incident off the wall, but a
real outage can't pin a stale "active" on the display forever.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

import requests

from .. import __version__, config

logger = logging.getLogger("bluebird-kiosk.incident")

# Cloud poll cadence. The loopback page poll is faster (~2s) and just reads this
# cache, so end-to-end reaction is roughly POLL_INTERVAL_SEC + the page interval.
POLL_INTERVAL_SEC = 3
# Per-request network timeout — short, since we re-poll continuously.
REQUEST_TIMEOUT_SEC = 8
# If a poll fails while we were showing an ACTIVE incident, keep showing it for
# at most this long before failing closed to inactive.
STALE_ACTIVE_GRACE_SEC = 30

_INACTIVE: Dict[str, Any] = {"active": False, "is_active": False}


class IncidentPoller:
    """Owns a daemon thread that keeps ``current()`` fresh from the cloud."""

    def __init__(self, *, interval_sec: int = POLL_INTERVAL_SEC) -> None:
        self._interval = max(1, int(interval_sec))
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = dict(_INACTIVE)
        self._last_active_at: float = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def current(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, state: Dict[str, Any]) -> None:
        with self._lock:
            self._state = dict(state)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="incident-poller", daemon=True
        )
        self._thread.start()
        logger.info("incident poller started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # never let the loop die on an unexpected error
                logger.exception("incident poll loop error")
            self._stop.wait(self._interval)

    def poll_once(self) -> Dict[str, Any]:
        """Do one cloud poll and update the cache. Returns the new state."""
        cfg = config.read_config()
        backend = (cfg.get("BLUEBIRD_BACKEND") or "").rstrip("/")
        token = config.read_license_token()
        if not backend or not token:
            # Not enrolled / not configured — nothing to show, no network call.
            self._set(_INACTIVE)
            return dict(_INACTIVE)
        try:
            resp = requests.get(
                f"{backend}/api/public/kiosk/active-incident",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": f"BlueBirdKiosk/{__version__}",
                },
                timeout=REQUEST_TIMEOUT_SEC,
                # Never follow a redirect with the bearer attached: requests strips
                # Authorization on a cross-host hop. /api/public/ isn't redirected
                # today, but pinning this makes the invariant explicit regardless of
                # proxy config — any 3xx is caught by the status check below.
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.debug("incident poll: network error: %s", exc)
            return self._fail_closed()
        if resp.status_code != 200:
            # 401/403 (token/tenant issue) or any other error → no modal.
            logger.debug("incident poll: backend status=%s", resp.status_code)
            return self._fail_closed()
        try:
            data = resp.json()
        except ValueError:
            logger.debug("incident poll: non-JSON body")
            return self._fail_closed()
        if isinstance(data, dict) and bool(data.get("active")):
            # Update the active timestamp + cache atomically under one lock so
            # _last_active_at stays consistent even if poll_once is ever called
            # off the single poller thread.
            with self._lock:
                self._last_active_at = time.monotonic()
                self._state = dict(data)
            return dict(data)
        self._set(_INACTIVE)
        return dict(_INACTIVE)

    def _fail_closed(self) -> Dict[str, Any]:
        """On a failed poll keep a recently-active incident on screen briefly,
        otherwise go inactive."""
        with self._lock:
            was_active = bool(self._state.get("active"))
            if was_active and (
                time.monotonic() - self._last_active_at < STALE_ACTIVE_GRACE_SEC
            ):
                return dict(self._state)
        self._set(_INACTIVE)
        return dict(_INACTIVE)
