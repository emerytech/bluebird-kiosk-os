"""Guard the systemd unit hardening vs. the control-plane writes the services make.

Regression for the heartbeat EROFS bug: bluebird-heartbeat.service ran with
ProtectSystem=strict but no ReadWritePaths, so /etc/bluebird (the Beacon signage
DISPLAY_MODE/SIGNAGE_URL flip) and /var/lib/bluebird-kiosk (the cached power
schedule) were read-only in its namespace — every write failed with EROFS and the
kiosk could never switch to a bound Beacon display. The sibling writer units
(admin, kiosk-sync) already carried ReadWritePaths; heartbeat was the omission.
"""
from __future__ import annotations

import configparser
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
_UNIT_DIR = _ROOT / "build/live-build/config/includes.chroot/etc/systemd/system"


def _service_section(unit_name: str) -> dict:
    # systemd units are INI-like; allow duplicate keys and keep them lenient.
    cp = configparser.RawConfigParser(strict=False)
    cp.optionxform = str  # preserve case
    cp.read(_UNIT_DIR / unit_name, encoding="utf-8")
    return dict(cp.items("Service")) if cp.has_section("Service") else {}


def test_heartbeat_can_write_its_control_plane_paths():
    svc = _service_section("bluebird-heartbeat.service")
    assert svc.get("ProtectSystem") == "strict"
    rwp = svc.get("ReadWritePaths", "")
    # Both the signage-assignment target (kiosk.conf) and the power-schedule cache.
    assert "/etc/bluebird" in rwp, "heartbeat cannot persist DISPLAY_MODE/SIGNAGE_URL -> Beacon signage dead"
    assert "/var/lib/bluebird-kiosk" in rwp, "heartbeat cannot cache the power schedule"


def test_strict_writer_units_declare_readwrite_paths():
    # Any unit hardened with ProtectSystem=strict that we know writes control-plane
    # state must declare ReadWritePaths, or those writes silently EROFS.
    for unit in ("bluebird-heartbeat.service", "bluebird-admin.service",
                 "bluebird-kiosk-sync.service"):
        svc = _service_section(unit)
        if svc.get("ProtectSystem") == "strict":
            assert svc.get("ReadWritePaths", "").strip(), f"{unit}: strict but no ReadWritePaths"
