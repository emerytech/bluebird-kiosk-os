"""Loopback CORS on the local kiosk server.

The cloud Legacy Wall page (HTTPS) reads loopback GET routes cross-origin via
fetch() — above all the active-incident takeover poll. Without an
Access-Control-Allow-Origin header echoed for the trusted wall origins, the
browser DISCARDS the response (the request still reaches the server → 200 in the
access log), the fetch rejects, and the emergency takeover silently never shows.
These tests guard that regression: the takeover only renders if the loopback
GET carries ACAO for the configured wall origins.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3]))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUEBIRD_LOCAL_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("BLUEBIRD_KIOSK_CONF", str(tmp_path / "kiosk.conf"))
    monkeypatch.setenv("BLUEBIRD_LICENSE_TOKEN", str(tmp_path / "license.token"))
    from bluebird_kiosk.server import create_app  # noqa: WPS433

    return TestClient(create_app())


# Every host a kiosk's Chromium may run the wall on (dashed/canonical/www +
# the white-label LW domain). All must get ACAO or the takeover breaks there.
TRUSTED_ORIGINS = [
    "https://bluebirdalerts.com",
    "https://www.bluebirdalerts.com",
    "https://bluebird-alerts.com",
    "https://www.bluebird-alerts.com",
    "https://legacy.ets3d.com",
]


@pytest.mark.parametrize("origin", TRUSTED_ORIGINS)
def test_active_incident_echoes_acao_for_trusted_origins(client, origin):
    r = client.get("/legacy-wall/api/active-incident", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin
    assert "Origin" in (r.headers.get("vary") or "")


def test_active_incident_no_acao_for_untrusted_origin(client):
    r = client.get(
        "/legacy-wall/api/active-incident",
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") is None


def test_active_incident_no_acao_without_origin(client):
    # Same-origin / curl (no Origin header) must not get a wildcard ACAO.
    r = client.get("/legacy-wall/api/active-incident")
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") is None


def test_cors_covers_media_listing_route(client):
    # The wall also fetches the slideshow-media listing cross-origin; the
    # /legacy-wall/* middleware should cover it too.
    r = client.get("/legacy-wall/api/media", headers={"Origin": "https://bluebirdalerts.com"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://bluebirdalerts.com"
