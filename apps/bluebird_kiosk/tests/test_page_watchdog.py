"""Wedged-page detection: the local server tracks page activity and the watchdog
restarts the session when the displayed page stops driving it (e.g. stuck on a
Cloudflare error page after a transient server drop)."""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))  # apps/ → makes `bluebird_kiosk` importable

from bluebird_kiosk.services import watchdog  # noqa: E402


@pytest.fixture
def kiosk_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUEBIRD_LOCAL_CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("BLUEBIRD_KIOSK_CONF", str(tmp_path / "kiosk.conf"))
    monkeypatch.setenv("BLUEBIRD_LICENSE_TOKEN", str(tmp_path / "license.token"))
    from bluebird_kiosk.server import create_app
    return create_app()


# ── local server: activity tracking + /_health ──────────────────────────────

def test_health_reports_page_idle_sec(kiosk_app):
    client = TestClient(kiosk_app)
    body = client.get("/_health").json()
    assert body["status"] == "ok"
    assert isinstance(body["page_idle_sec"], int)
    assert body["page_idle_sec"] >= 0


def test_page_request_resets_idle_but_health_does_not(kiosk_app):
    # Simulate a page that's been silent for a while.
    kiosk_app.state.last_page_activity = time.monotonic() - 500
    client = TestClient(kiosk_app)

    # /_health must NOT count as page activity (else the watchdog's own probe
    # would mask a wedged page).
    assert client.get("/_health").json()["page_idle_sec"] >= 400

    # A real page-driven request resets the freshness clock.
    client.get("/legacy-wall/api/active-incident")
    assert client.get("/_health").json()["page_idle_sec"] < 5


# ── watchdog: _local_health parsing + page_fresh gating ─────────────────────

def _fake_run(returncode, stdout=b""):
    def _run(*a, **k):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout)
    return _run


def test_local_health_parses_idle(monkeypatch):
    monkeypatch.setattr(watchdog.subprocess, "run",
                        _fake_run(0, b'{"status":"ok","page_idle_sec":42}'))
    assert watchdog._local_health() == (True, 42)


def test_local_health_failure_and_bad_json(monkeypatch):
    monkeypatch.setattr(watchdog.subprocess, "run", _fake_run(7, b""))
    assert watchdog._local_health() == (False, None)
    monkeypatch.setattr(watchdog.subprocess, "run", _fake_run(0, b"not json"))
    assert watchdog._local_health() == (True, None)  # fail-safe: fresh


def test_page_fresh_is_critical(monkeypatch):
    monkeypatch.setattr(watchdog, "_process_running", lambda *a, **k: True)
    monkeypatch.setattr(watchdog, "_systemctl_is_active", lambda *a, **k: True)

    # Page polling normally → fresh → healthy.
    monkeypatch.setattr(watchdog, "_local_health", lambda: (True, 5))
    all_pass, checks = watchdog._run_checks()
    assert checks["page_fresh"] is True and all_pass is True

    # Page wedged (idle well past threshold) → not fresh → unhealthy.
    monkeypatch.setattr(watchdog, "_local_health",
                        lambda: (True, watchdog.PAGE_STALE_SEC + 60))
    all_pass, checks = watchdog._run_checks()
    assert checks["page_fresh"] is False and all_pass is False

    # Local server down → local_health fails, page_fresh stays neutral (fresh).
    monkeypatch.setattr(watchdog, "_local_health", lambda: (False, None))
    all_pass, checks = watchdog._run_checks()
    assert checks["local_health"] is False and checks["page_fresh"] is True


def test_chromium_check_keys_on_profile_dir_not_https(monkeypatch):
    # The kiosk legitimately runs --app=http://127.0.0.1 (offline fallback, firstboot, Beacon
    # local signage), so the chromium liveness check must key on the PROFILE DIR, not the URL
    # scheme — otherwise a healthy offline board reads as "chromium dead" and reboot-loops.
    seen = []
    monkeypatch.setattr(watchdog, "_process_running",
                        lambda pat, **k: (seen.append(pat), True)[1])
    monkeypatch.setattr(watchdog, "_local_health", lambda: (True, 0))
    monkeypatch.setattr(watchdog, "_systemctl_is_active", lambda *a, **k: True)
    watchdog._run_checks()
    chromium_pats = [p for p in seen if "chromium" in p]
    assert chromium_pats, "no chromium check ran"
    assert any("kiosk-chromium" in p for p in chromium_pats)        # keys on the profile dir
    assert not any("--app=https" in p for p in chromium_pats)       # not the URL scheme


def test_configured_path_guards_firstboot():
    # The watchdog must know the firstboot marker so it never 'recovers' an unconfigured kiosk.
    assert str(watchdog.CONFIGURED_PATH) == "/etc/bluebird/configured"
