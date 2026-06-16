"""Tests for IncidentPoller + the /legacy-wall/api/active-incident loopback route.

The poller calls the cloud `/api/public/kiosk/active-incident` with the device
bearer token and caches the result; it FAILS CLOSED on any error. The loopback
route just returns the cache. We mock requests + config so no real network or
device filesystem is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))  # apps/ → makes `bluebird_kiosk` importable

from bluebird_kiosk.services import incident_poller as ip  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self._json = json_body

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _configure(monkeypatch, *, backend: str = "https://bluebird-alerts.com", token: str = "tok") -> None:
    monkeypatch.setattr(ip.config, "read_config", lambda: {"BLUEBIRD_BACKEND": backend})
    monkeypatch.setattr(ip.config, "read_license_token", lambda: token)


# ── poll_once ────────────────────────────────────────────────────────────────


def test_poll_once_no_token_is_inactive_without_network(monkeypatch) -> None:
    def _boom(*a, **k):
        raise AssertionError("must not hit the network without a license token")

    monkeypatch.setattr(ip.requests, "get", _boom)
    monkeypatch.setattr(ip.config, "read_config", lambda: {"BLUEBIRD_BACKEND": "https://x"})
    monkeypatch.setattr(ip.config, "read_license_token", lambda: None)

    state = ip.IncidentPoller().poll_once()
    assert state["active"] is False


def test_poll_once_active_caches_payload_and_sends_bearer(monkeypatch) -> None:
    _configure(monkeypatch)
    captured: Dict[str, Any] = {}

    def _get(url, headers=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["auth"] = (headers or {}).get("Authorization")
        captured["allow_redirects"] = kwargs.get("allow_redirects")
        return FakeResponse(200, {"active": True, "is_active": True, "message": "LOCKDOWN"})

    monkeypatch.setattr(ip.requests, "get", _get)

    poller = ip.IncidentPoller()
    state = poller.poll_once()
    assert state["active"] is True
    assert state["message"] == "LOCKDOWN"
    assert poller.current()["active"] is True
    # Hits the /api/public/ path (NOT a /{slug}/ path that would redirect+strip auth).
    assert captured["url"].endswith("/api/public/kiosk/active-incident")
    assert captured["auth"] == "Bearer tok"
    # Never follows a redirect with the bearer attached.
    assert captured["allow_redirects"] is False


def test_poll_once_401_fails_closed(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(ip.requests, "get", lambda *a, **k: FakeResponse(401, None))
    assert ip.IncidentPoller().poll_once()["active"] is False


def test_poll_once_inactive_payload(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(ip.requests, "get", lambda *a, **k: FakeResponse(200, {"active": False}))
    assert ip.IncidentPoller().poll_once()["active"] is False


def test_network_error_keeps_recent_active_then_fails_closed(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        ip.requests, "get", lambda *a, **k: FakeResponse(200, {"active": True, "message": "X"})
    )
    poller = ip.IncidentPoller()
    assert poller.poll_once()["active"] is True

    # Network drops — within the grace window we keep showing the live incident.
    def _err(*a, **k):
        raise ip.requests.RequestException("down")

    monkeypatch.setattr(ip.requests, "get", _err)
    assert poller.poll_once()["active"] is True  # still active (grace)

    # Force the grace window to expire → fail closed to inactive.
    poller._last_active_at -= ip.STALE_ACTIVE_GRACE_SEC + 1
    assert poller.poll_once()["active"] is False


def test_degraded_503_keeps_active_while_clean_inactive_clears(monkeypatch) -> None:
    """A 503 (backend degraded) during a live incident routes through the grace
    window and KEEPS the takeover; a clean 200 {active:false} is authoritative and
    clears it immediately."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        ip.requests, "get", lambda *a, **k: FakeResponse(200, {"active": True, "message": "X"})
    )
    poller = ip.IncidentPoller()
    assert poller.poll_once()["active"] is True
    # Backend degraded (503) — grace holds the live incident on the wall.
    monkeypatch.setattr(
        ip.requests, "get", lambda *a, **k: FakeResponse(503, {"active": False, "degraded": True})
    )
    assert poller.poll_once()["active"] is True
    # A clean 200 {active:false} authoritatively clears it.
    monkeypatch.setattr(ip.requests, "get", lambda *a, **k: FakeResponse(200, {"active": False}))
    assert poller.poll_once()["active"] is False


# ── loopback route ───────────────────────────────────────────────────────────


@pytest.fixture
def kiosk_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUEBIRD_LOCAL_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("BLUEBIRD_KIOSK_CONF", str(tmp_path / "kiosk.conf"))
    monkeypatch.setenv("BLUEBIRD_LICENSE_TOKEN", str(tmp_path / "license.token"))
    from bluebird_kiosk.server import create_app  # deferred: env must be set first

    return create_app()


def test_loopback_returns_poller_cache(kiosk_app) -> None:
    # Poller is created but NOT started (no thread); seed its cache directly.
    kiosk_app.state.incident_poller._set(
        {"active": True, "is_active": True, "message": "DRILL"}
    )
    client = TestClient(kiosk_app)
    resp = client.get("/legacy-wall/api/active-incident")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["message"] == "DRILL"


def test_loopback_inactive_by_default(kiosk_app) -> None:
    client = TestClient(kiosk_app)
    resp = client.get("/legacy-wall/api/active-incident")
    assert resp.status_code == 200
    assert resp.json()["active"] is False
