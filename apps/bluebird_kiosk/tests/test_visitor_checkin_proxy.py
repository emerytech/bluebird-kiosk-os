"""VMS Phase 2b: the on-device visitor check-in loopback proxy.

The cloud /{slug}/visitor-kiosk page POSTs a check-in to 127.0.0.1:7311; the local
admin server injects THIS kiosk's license bearer and forwards to the cloud so the token
never touches the page. Invariants under test: bearer injected, forwarded to the
/api/public/ endpoint with allow_redirects=False (308 strips Authorization), cloud
response relayed, CORS + Private-Network headers present, and 403 when unenrolled.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3]))

ORIGIN = "https://bluebirdalerts.com"


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUEBIRD_LOCAL_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("BLUEBIRD_KIOSK_CONF", str(tmp_path / "kiosk.conf"))
    monkeypatch.setenv("BLUEBIRD_LICENSE_TOKEN", str(tmp_path / "license.token"))
    from bluebird_kiosk.server import create_app  # noqa: WPS433

    return create_app()


def _capture_post(monkeypatch):
    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        return _FakeResp(200, {"ok": True, "badge_number": "V-007", "visitor_name": "Pat"})

    monkeypatch.setattr("bluebird_kiosk.server.requests.post", fake_post)
    return calls


def _enroll(monkeypatch, tmp_path, token="tok-abc123"):
    """Point the license-token path at a tmp file and write a bearer (avoids
    the module-level /etc/bluebird path frozen at import time)."""
    from bluebird_kiosk import config

    tok = tmp_path / "license.token"
    tok.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "LICENSE_TOKEN_PATH", tok)


def test_checkin_injects_bearer_and_forwards(app_env, tmp_path, monkeypatch):
    _enroll(monkeypatch, tmp_path)
    calls = _capture_post(monkeypatch)
    client = TestClient(app_env)

    r = client.post(
        "/legacy-wall/api/visitor/checkin",
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        json={"visitor_name": "Pat", "visiting_whom": "Ms. Lee", "purpose": "Meeting"},
    )
    assert r.status_code == 200, r.text
    # Cloud response relayed verbatim.
    assert r.json() == {"ok": True, "badge_number": "V-007", "visitor_name": "Pat"}
    # Forwarded to the /api/public/ endpoint (redirect-safe) with the bearer.
    assert calls["url"].endswith("/api/public/kiosk/visitor/checkin")
    assert calls["kwargs"]["headers"]["Authorization"] == "Bearer tok-abc123"
    assert calls["kwargs"]["allow_redirects"] is False
    # Body forwarded; tenant_id is NOT sent (derived cloud-side from the bearer).
    assert calls["kwargs"]["json"]["visitor_name"] == "Pat"
    assert "tenant_id" not in calls["kwargs"]["json"]
    # CORS so the HTTPS page isn't blocked reading the response.
    assert r.headers.get("access-control-allow-origin") == ORIGIN


def test_checkin_relays_cloud_error_status(app_env, tmp_path, monkeypatch):
    _enroll(monkeypatch, tmp_path)

    def fake_post(url, **kwargs):
        return _FakeResp(429, {"error": "rate_limited"})

    monkeypatch.setattr("bluebird_kiosk.server.requests.post", fake_post)
    client = TestClient(app_env)
    r = client.post(
        "/legacy-wall/api/visitor/checkin",
        headers={"Origin": ORIGIN},
        json={"visitor_name": "Pat"},
    )
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited"


def test_checkin_403_when_not_enrolled(app_env, monkeypatch):
    # No license token written → the proxy must not call the cloud.
    called = {"n": 0}

    def fake_post(url, **kwargs):
        called["n"] += 1
        return _FakeResp(200, {})

    monkeypatch.setattr("bluebird_kiosk.server.requests.post", fake_post)
    client = TestClient(app_env)
    r = client.post(
        "/legacy-wall/api/visitor/checkin",
        headers={"Origin": ORIGIN},
        json={"visitor_name": "Pat"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "not_enrolled"
    assert called["n"] == 0


def test_checkin_preflight_allows_private_network(app_env):
    client = TestClient(app_env)
    r = client.options("/legacy-wall/api/visitor/checkin", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-private-network") == "true"
    assert r.headers.get("access-control-allow-origin") == ORIGIN
