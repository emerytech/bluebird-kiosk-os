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
    """Return the URL the kiosk Chromium should load on boot.

    Points at the LOCAL renderer at 127.0.0.1:7311/legacy-wall, which
    reads from the on-disk media cache (/var/lib/bluebird-kiosk/media)
    populated by bluebird-kiosk-sync.service. This makes image
    transitions instant and survives WAN outages.

    The cloud `backend` and `slug` args are kept in the signature for
    API stability — existing callers (firstboot finalize, admin
    /admin/kiosk/slug) pass them through. They are validated upstream
    (BLUEBIRD_BACKEND must be set, slug must resolve) but no longer
    appear in the URL Chromium loads.

    Field-confirmed (2026-06-04): pointing Chromium at the cloud URL
    was forcing every slide transition through WAN even though we had
    761 MB of media already cached locally. Switching here drops
    per-image latency from hundreds of ms over WiFi to 25-170 ms from
    local SSD.

    The sync service still talks to the cloud via BLUEBIRD_BACKEND +
    SCHOOL_SLUG read from kiosk.conf directly — that's how new media
    reaches the cache.
    """
    backend = (backend or "").rstrip("/")
    slug = (slug or "").strip("/")
    # We still require both values to be set — they're validated during
    # firstboot to confirm the tenant exists and is reachable, and the
    # sync loop won't run without them. The legacy URL itself is local.
    if not backend or not slug:
        return ""
    return "http://127.0.0.1:7311/legacy-wall"


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
