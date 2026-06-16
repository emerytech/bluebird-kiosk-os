"""Admin overlay shows kiosk-os change notes (proxied from the cloud) before an
operator confirms an update."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))


@pytest.fixture
def kiosk_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUEBIRD_LOCAL_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("BLUEBIRD_KIOSK_CONF", str(tmp_path / "kiosk.conf"))
    monkeypatch.setenv("BLUEBIRD_LICENSE_TOKEN", str(tmp_path / "license.token"))
    from bluebird_kiosk.server import create_app
    return create_app()


def _auth(app):
    app.state.admin_sessions["tok"] = time.monotonic() + 999
    return {"X-Admin-Session": "tok"}


class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._p = payload
    def json(self):
        return self._p


def test_release_notes_proxies_cloud(kiosk_app, monkeypatch):
    from bluebird_kiosk import server as srv
    monkeypatch.setattr(srv.requests, "get",
                        lambda *a, **k: _Resp(200, {"notes": "## new\n- a thing", "version": "abc123"}))
    r = TestClient(kiosk_app).get("/admin/system/release-notes", headers=_auth(kiosk_app))
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True and "a thing" in b["notes"]
    assert b["available_version"] == "abc123"      # cloud "version" → available
    assert "current_version" in b                  # None in the test env (no stamp file)


def test_release_notes_graceful_on_network_error(kiosk_app, monkeypatch):
    from bluebird_kiosk import server as srv
    def boom(*a, **k):
        raise RuntimeError("dns fail")
    monkeypatch.setattr(srv.requests, "get", boom)
    r = TestClient(kiosk_app).get("/admin/system/release-notes", headers=_auth(kiosk_app))
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is False and "dns fail" in b["error"]


def test_release_notes_requires_admin(kiosk_app):
    r = TestClient(kiosk_app).get("/admin/system/release-notes")
    assert r.status_code == 401
