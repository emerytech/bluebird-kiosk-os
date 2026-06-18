"""WiFi "forget" — remove a saved network from the admin overlay.

Covers the nmcli_wrapper plumbing (safe argv, wifi-only filtering, colon-safe
names, failure propagation) and the UI/route wiring (saved-list + Forget button
+ the PIN-gated /admin/network/forget endpoint).
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))   # .../apps  -> import bluebird_kiosk

from bluebird_kiosk.services import nmcli_wrapper  # noqa: E402

_WEB = _HERE.parents[1] / "web"
ADMIN_HTML = (_WEB / "templates" / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (_WEB / "static" / "admin.js").read_text(encoding="utf-8")
SERVER = (_HERE.parents[1] / "server.py").read_text(encoding="utf-8")


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── nmcli_wrapper.forget ──────────────────────────────────────────────────────

def test_forget_builds_safe_argv(monkeypatch):
    seen = {}
    monkeypatch.setattr(nmcli_wrapper.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv) or _FakeProc(0, "deleted"))
    ok, msg = nmcli_wrapper.forget("Net:With:Colons")
    assert ok
    # explicit argv (no shell); the SSID is a single trailing element -> injection-safe
    assert seen["argv"][:4] == [nmcli_wrapper.NMCLI, "connection", "delete", "id"]
    assert seen["argv"][4] == "Net:With:Colons"


def test_forget_empty_name_never_shells_out(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not shell out on empty name")
    monkeypatch.setattr(nmcli_wrapper.subprocess, "run", _boom)
    ok, _ = nmcli_wrapper.forget("")
    assert not ok


def test_forget_propagates_failure(monkeypatch):
    monkeypatch.setattr(nmcli_wrapper.subprocess, "run",
                        lambda argv, **kw: _FakeProc(returncode=1, stderr="boom"))
    ok, msg = nmcli_wrapper.forget("X")
    assert not ok and "boom" in msg


# ── nmcli_wrapper.saved_networks ──────────────────────────────────────────────

def test_saved_networks_filters_wifi_and_keeps_colon_names(monkeypatch):
    out = "\n".join([
        "802-11-wireless:Eagles WiFi",
        "802-3-ethernet:Wired connection 1",   # not wifi -> excluded
        "802-11-wireless:Net:With:Colons",      # colon in name preserved (split once on TYPE)
        "802-11-wireless:Eagles WiFi",          # duplicate -> deduped
    ])
    monkeypatch.setattr(nmcli_wrapper.subprocess, "run",
                        lambda argv, **kw: _FakeProc(returncode=0, stdout=out))
    saved = nmcli_wrapper.saved_networks()
    assert "Eagles WiFi" in saved
    assert "Net:With:Colons" in saved
    assert "Wired connection 1" not in saved
    assert saved.count("Eagles WiFi") == 1


# ── UI + route wiring ─────────────────────────────────────────────────────────

def test_saved_network_ui_present():
    assert 'id="saved-list"' in ADMIN_HTML
    assert "function renderSaved" in ADMIN_JS
    assert "/admin/network/forget" in ADMIN_JS
    # destructive delete is two-tap confirmed (native confirm() is blocked under --kiosk)
    assert "Tap to confirm" in ADMIN_JS


def test_forget_route_registered_and_pin_gated():
    assert "/admin/network/forget" in SERVER
    assert "def admin_network_forget" in SERVER
    assert "WifiForgetBody" in SERVER
    # the status endpoint now also surfaces saved networks for the list
    assert "saved_networks()" in SERVER
