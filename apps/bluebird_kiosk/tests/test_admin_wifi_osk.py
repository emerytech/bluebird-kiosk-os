"""Guard: the PIN-locked admin overlay must support touch-only text entry.

Regression for the demo-day bug — tapping a text/password field (WiFi password,
slug, PIN, brightness) showed no on-screen keyboard because admin.html never
loaded osk.js, the scanned-network connect used a native prompt() (suppressed by
Chromium --kiosk), and various inputs lacked data-osk. The fix mirrors the proven
firstboot pattern: load osk.js, replace prompt() with in-page modals, and tag
EVERY text/password input with data-osk.
"""
import re
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


def test_change_slug_and_pin_use_text_modal_not_blocked_prompt():
    # Generic #text-modal present with an OSK-bound input.
    assert 'id="text-modal"' in ADMIN_HTML
    assert 'id="text-modal-input"' in ADMIN_HTML
    assert "function askText" in ADMIN_JS
    # change-slug / change-PIN no longer use native prompt() (blocked under --kiosk)
    assert "prompt('New school slug" not in ADMIN_JS
    assert "prompt('New 6-digit PIN" not in ADMIN_JS
    assert 'prompt("New school slug' not in ADMIN_JS
    assert 'prompt("New 6-digit PIN' not in ADMIN_JS


def test_no_native_prompt_calls_remain_in_admin_js():
    # Strip line comments, then assert no real prompt() call survives (all are
    # replaced by in-page modals; native prompt() is suppressed under --kiosk).
    code = "\n".join(
        line.split("//", 1)[0] for line in ADMIN_JS.splitlines()
    )
    assert "prompt(" not in code


def test_every_admin_text_input_binds_the_osk():
    # Completeness guard: every text-entry <input> in the admin overlay must
    # carry data-osk, or it gets NO on-screen keyboard on a touch-only kiosk.
    TEXT_TYPES = {"text", "password", "search", "email", "tel", "number"}
    for tag in re.findall(r"<input\b[^>]*>", ADMIN_HTML):
        m = re.search(r'type="([a-z]+)"', tag)
        itype = m.group(1) if m else "text"   # HTML default type is text
        if itype in TEXT_TYPES:
            assert "data-osk=" in tag, f"text input lacks data-osk: {tag}"
