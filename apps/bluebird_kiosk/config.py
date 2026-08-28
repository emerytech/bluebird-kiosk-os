"""Read/write of /etc/bluebird/kiosk.conf.

The file is a simple `KEY=value` shell-source format so it can also be
referenced from systemd `EnvironmentFile=` directives.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
from pathlib import Path
from typing import Dict, Optional

CONF_PATH = Path(os.environ.get("BLUEBIRD_KIOSK_CONF", "/etc/bluebird/kiosk.conf"))
CONFIGURED_FLAG = Path("/etc/bluebird/configured")
PIN_HASH_PATH = Path("/etc/bluebird/admin.pin")
LICENSE_TOKEN_PATH = Path(
    os.environ.get("BLUEBIRD_LICENSE_TOKEN", "/etc/bluebird/license.token")
)

_KV_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")

_DEFAULTS: Dict[str, str] = {
    # Dashed bluebird-alerts.com is the safer default because that's the
    # domain school IT departments historically whitelisted in their
    # content filters. The canonical bluebirdalerts.com is what the
    # backend actually answers from, reached via a 308 redirect — clients
    # using curl -L / Python requests follow it transparently. Operators
    # on a network that prefers the canonical host can edit
    # BLUEBIRD_BACKEND in /etc/bluebird/kiosk.conf.
    "BLUEBIRD_BACKEND": "https://bluebird-alerts.com",
    "SCHOOL_SLUG": "",
    "LEGACY_WALL_URL": "",
    "DEVICE_ID": "",
    "ADMIN_PORT": "7311",
    "HEARTBEAT_INTERVAL_SEC": "60",
    # Beacon signage (fleet delivery). DISPLAY_MODE is "legacy_wall" (default) or
    # "signage"; SIGNAGE_URL is the bound Beacon display's cloud URL. Both are
    # written by the heartbeat from the backend's `display_assignment` and read by
    # launch-kiosk-chromium. An unbound kiosk stays in legacy_wall mode untouched.
    "DISPLAY_MODE": "legacy_wall",
    "SIGNAGE_URL": "",
    # VMS Phase 2b — visitor check-in kiosk. DISPLAY_MODE="visitor" + VISITOR_URL (the
    # cloud /{slug}/visitor-kiosk page) render the check-in page, armed like signage so
    # the emergency takeover still fires. Set in kiosk.conf for a dedicated visitor kiosk
    # (derive_visitor_url computes it from BLUEBIRD_BACKEND + SCHOOL_SLUG).
    "VISITOR_URL": "",
    # Visitor badge print agent (VMS). When "1" (ONE designated box per school), the PrintAgent
    # pulls pending badge jobs from the cloud and sends ZPL to the configured network printer.
    # Default off so only the chosen kiosk on the LAN acts as the print bridge.
    "PRINT_AGENT_ENABLED": "0",
}


def read_config() -> Dict[str, str]:
    """Return the current kiosk.conf as a dict, with defaults filled in."""
    values: Dict[str, str] = dict(_DEFAULTS)
    if CONF_PATH.exists():
        for line in CONF_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _KV_RE.match(stripped)
            if not m:
                continue
            key, raw = m.group(1), m.group(2)
            values[key] = raw.strip().strip('"').strip("'")
    return values


def write_config(values: Dict[str, str]) -> None:
    """Atomically write kiosk.conf with the merged values. Values are
    shell-quoted so they're safe to `source` from bash launchers running
    under `set -eu` (e.g. a TENANT_NAME with a space would otherwise
    split into two words and abort the launcher)."""
    merged = read_config()
    merged.update({k: ("" if v is None else str(v)) for k, v in values.items()})
    lines = ["# BlueBird Kiosk OS — runtime configuration (written by control plane)\n"]
    for key, value in merged.items():
        lines.append(f"{key}={shlex.quote(value)}\n")
    tmp = CONF_PATH.with_suffix(".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(CONF_PATH)


def ensure_device_id() -> str:
    """Return the persisted DEVICE_ID; generate and persist one if missing."""
    cfg = read_config()
    if cfg.get("DEVICE_ID"):
        return cfg["DEVICE_ID"]
    device_id = f"bbk-{secrets.token_hex(8)}"
    write_config({"DEVICE_ID": device_id})
    return device_id


def mark_configured() -> None:
    CONFIGURED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    CONFIGURED_FLAG.write_text("configured\n", encoding="utf-8")


def is_configured() -> bool:
    return CONFIGURED_FLAG.exists()


def derive_legacy_wall_url(backend: str, slug: str) -> str:
    """Return the cloud URL the kiosk Chromium loads.

    Chromium loads the rich cloud-rendered Legacy Wall page (sidebar,
    collections, lightbox, touch nav, etc) for visual + functional
    parity with non-kiosk viewers. Per-image latency is then minimized
    by a bundled Chromium extension (`bluebird_kiosk_cache_ext`) that
    intercepts requests to `<backend>/<slug>/legacy-wall/media/<id>/...`
    and redirects them to the local renderer at 127.0.0.1:7311, which
    serves bytes out of /var/lib/bluebird-kiosk/media. Cache misses
    fall back through to the cloud via a 302 from the local server.

    This was originally a redirect to 127.0.0.1 (commit 5734efd) so
    Chromium loaded a minimal local-only slideshow. That replaced the
    cloud dashboard wholesale and lost the touch UI / sidebar /
    sports widgets. The cache-extension approach (this commit) keeps
    the dashboard intact and only redirects the heavy media requests.
    """
    backend = (backend or "").rstrip("/")
    slug = (slug or "").strip("/")
    if not backend or not slug:
        return ""
    return f"{backend}/{slug}/legacy-wall"


def derive_visitor_url(backend: str, slug: str) -> str:
    """Return the cloud visitor check-in URL a visitor-mode kiosk loads
    (VMS Phase 2b): `<backend>/<slug>/visitor-kiosk`. The launcher appends
    ?kiosk_cache=1 (arming the emergency takeover); the page's check-in POST
    goes to the on-device loopback proxy which injects the license bearer."""
    backend = (backend or "").rstrip("/")
    slug = (slug or "").strip("/")
    if not backend or not slug:
        return ""
    return f"{backend}/{slug}/visitor-kiosk"


def derive_beacon_url(backend: str, slug: str, public_key: str) -> str:
    """Return the cloud URL a kiosk loads when bound to a Beacon signage display:
    GET <backend>/<slug>/beacon/d/<public_key> — a fullscreen 16:9 page.

    Like the Legacy Wall URL, it is a normal cloud-rendered page; the launcher
    appends ?kiosk_cache=1 to it, which (a) enables the bundled local image cache
    and (b) — critically — arms the emergency-takeover poller embedded in the
    page. Returns "" if any part is missing so the caller can fall back."""
    backend = (backend or "").rstrip("/")
    slug = (slug or "").strip("/")
    public_key = (public_key or "").strip("/")
    if not backend or not slug or not public_key:
        return ""
    return f"{backend}/{slug}/beacon/d/{public_key}"


def get_admin_pin_hash() -> Optional[bytes]:
    if not PIN_HASH_PATH.exists():
        return None
    return PIN_HASH_PATH.read_bytes()


def set_admin_pin_hash(hashed: bytes) -> None:
    PIN_HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    PIN_HASH_PATH.write_bytes(hashed)
    try:
        os.chmod(PIN_HASH_PATH, 0o600)
    except OSError:
        pass


def read_license_token() -> Optional[str]:
    if not LICENSE_TOKEN_PATH.exists():
        return None
    try:
        return LICENSE_TOKEN_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_license_token(token: str) -> None:
    LICENSE_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LICENSE_TOKEN_PATH.write_text(token.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(LICENSE_TOKEN_PATH, 0o600)
    except OSError:
        pass


def has_license() -> bool:
    return LICENSE_TOKEN_PATH.exists()
