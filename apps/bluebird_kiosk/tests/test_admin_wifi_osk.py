"""Guard: the PIN-locked admin overlay must support touch-only WiFi entry.

Regression for the demo-day bug — tapping the WiFi password field showed no
on-screen keyboard because admin.html never loaded osk.js AND the scanned-network
connect used a native prompt() (which Chromium --kiosk suppresses). The fix
mirrors the proven firstboot pattern: load osk.js, use an in-page modal whose
password input carries data-osk, and tag the hidden-network inputs too.
"""
from pathlib import Path

_WEB = Path(__file__).resolve().parents[1] / "web"
ADMIN_HTML = (_WEB / "templates" / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (_WEB / "static" / "admin.js").read_text(encoding="utf-8")


def test_admin_overlay_loads_the_osk():
    # Without this script tag there is NO on-screen keyboard in the admin overlay.
    assert "/static/osk.js" in ADMIN_HTML


def test_admin_wifi_modal_present_with_osk_input():
    assert 'id="wifi-modal"' in ADMIN_HTML
    assert 'id="wifi-password-input"' in ADMIN_HTML
    # the modal password field must bind the on-screen keyboard
    assert 'data-osk="qwerty"' in ADMIN_HTML


def test_hidden_network_inputs_bind_the_osk():
    # the "Join hidden network" SSID + password fields each opt into the OSK
    assert 'data-osk="alphanum"' in ADMIN_HTML   # #hidden-ssid
    # two qwerty fields exist (modal password + #hidden-pass)
    assert ADMIN_HTML.count('data-osk="qwerty"') >= 2


def test_scanned_network_connect_uses_modal_not_blocked_prompt():
    assert "askWifiPassword" in ADMIN_JS
    # the native prompt() for WiFi (blocked under Chromium --kiosk) is gone
    assert "prompt(`Password for" not in ADMIN_JS
    assert "prompt('Password for" not in ADMIN_JS
