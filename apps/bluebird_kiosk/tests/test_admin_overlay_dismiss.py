"""The admin overlay must be dismissable from the PIN screen.

Regression for the stuck-PIN-screen bug: the top-bar ✕ used to call the
PIN-gated /admin/kiosk/return-to-kiosk route. Before login there is no
X-Admin-Session header, so the call 401'd, api() re-rendered the PIN screen,
and the overlay could not be closed at all — an accidental 5-finger gesture
left the kiosk stuck until someone rebooted it. The fix is a dedicated
/admin/kiosk/dismiss-overlay route that skips the PIN gate and ONLY closes
the overlay window (no slideshow relaunch, which stays PIN-gated).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[3]))

_WEB = _HERE.parents[1] / "web"
ADMIN_JS = (_WEB / "static" / "admin.js").read_text(encoding="utf-8")


@pytest.fixture
def app_and_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUEBIRD_LOCAL_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("BLUEBIRD_KIOSK_CONF", str(tmp_path / "kiosk.conf"))
    monkeypatch.setenv("BLUEBIRD_LICENSE_TOKEN", str(tmp_path / "license.token"))
    from bluebird_kiosk.server import create_app  # noqa: WPS433

    app = create_app()
    return app, TestClient(app)


@pytest.fixture
def system_spy(monkeypatch):
    """Record calls to the two window-management actions the routes invoke."""
    from bluebird_kiosk.services import system  # noqa: WPS433

    calls = {"close": 0, "reload": 0}

    def fake_close():
        calls["close"] += 1
        return True, "Admin overlay closed."

    def fake_reload():
        calls["reload"] += 1
        return True, "Kiosk display relaunched."

    monkeypatch.setattr(system, "close_admin_overlay", fake_close)
    monkeypatch.setattr(system, "reload_kiosk_display", fake_reload)
    return calls


def test_dismiss_overlay_needs_no_session(app_and_client, system_spy):
    # No X-Admin-Session header at all — the PIN screen's ✕ runs pre-auth.
    _, client = app_and_client
    r = client.post("/admin/kiosk/dismiss-overlay")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert system_spy["close"] == 1
    # Dismiss must NOT relaunch the slideshow — that recovery action is
    # PIN-gated on /admin/kiosk/return-to-kiosk.
    assert system_spy["reload"] == 0


def test_return_to_kiosk_still_requires_session(app_and_client, system_spy):
    # Auth-guard regression: the slideshow-relaunch route stays PIN-gated.
    _, client = app_and_client
    r = client.post("/admin/kiosk/return-to-kiosk")
    assert r.status_code == 401
    assert system_spy["close"] == 0
    assert system_spy["reload"] == 0


def test_return_to_kiosk_works_with_session(app_and_client, system_spy):
    app, client = app_and_client
    app.state.admin_sessions["tok-test"] = time.monotonic() + 60
    r = client.post(
        "/admin/kiosk/return-to-kiosk", headers={"X-Admin-Session": "tok-test"}
    )
    assert r.status_code == 200
    assert system_spy["close"] == 1
    assert system_spy["reload"] == 1


def test_close_button_uses_the_ungated_dismiss_action():
    # The ✕ must call dismissOverlay (ungated route), never returnToKiosk —
    # wiring it back to returnToKiosk silently reintroduces the stuck screen.
    assert "addEventListener('click', dismissOverlay)" in ADMIN_JS
    assert "btnCloseOverlay.addEventListener('click', returnToKiosk)" not in ADMIN_JS
    assert "'/admin/kiosk/dismiss-overlay'" in ADMIN_JS
