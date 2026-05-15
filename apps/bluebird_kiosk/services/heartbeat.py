"""Background heartbeat sender — POSTs once per minute to the BlueBird backend.

Run as a long-lived service via `python3 -m bluebird_kiosk.services.heartbeat`.

Each heartbeat carries:
  • Identification (slug, device id, hostname, OS + kiosk version)
  • Uptime (from /proc/uptime)
  • Resource snapshot (CPU load, memory, disk, sync cache size)

The resource fields let super-admin see at a glance which kiosks are
running hot, low on disk, or short on memory — without having to SSH
into each one or open the device admin overlay.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from .. import __version__, config


logger = logging.getLogger("bluebird-kiosk.heartbeat")


def _read_os_version() -> str:
    try:
        for line in open("/etc/os-release", encoding="utf-8"):
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "BlueBirdKioskOS unknown"


def _read_uptime() -> int:
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError):
        return 0


# ── Resource sampling helpers ────────────────────────────────────────────────
#
# All helpers return None on any failure — the heartbeat sender just leaves
# the field out of the payload. We never want a metric-collection bug to
# break the heartbeat itself.


def _read_loadavg_1min() -> Optional[float]:
    try:
        with open("/proc/loadavg", encoding="ascii") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_meminfo_mb() -> Tuple[Optional[int], Optional[int]]:
    """Returns (total_mb, used_mb). Used = total - available."""
    info: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                tokens = parts[1].strip().split()
                if not tokens:
                    continue
                try:
                    info[key] = int(tokens[0])  # always in kB
                except ValueError:
                    continue
    except OSError:
        return None, None
    total_kb = info.get("MemTotal")
    avail_kb = info.get("MemAvailable")
    if total_kb is None or avail_kb is None:
        return None, None
    total_mb = total_kb // 1024
    used_mb = max(0, (total_kb - avail_kb) // 1024)
    return total_mb, used_mb


def _read_disk_root_gb() -> Tuple[Optional[float], Optional[float]]:
    """Returns (total_gb, used_gb) for the root filesystem."""
    try:
        st = os.statvfs("/")
    except OSError:
        return None, None
    total_b = st.f_blocks * st.f_frsize
    free_b = st.f_bavail * st.f_frsize
    used_b = max(0, total_b - free_b)
    return round(total_b / (1024 ** 3), 2), round(used_b / (1024 ** 3), 2)


def _read_cache_size_mb() -> Optional[int]:
    """Disk used by the kiosk's local sync cache (media blobs + cache.db)."""
    base = Path("/var/lib/bluebird-kiosk")
    if not base.is_dir():
        return None
    total = 0
    try:
        for p in base.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
    except OSError:
        return None
    return int(total // (1024 * 1024))


def _read_kiosk_os_version() -> Optional[str]:
    try:
        return Path("/etc/bluebird/kiosk-os.version").read_text(
            encoding="utf-8"
        ).strip() or None
    except OSError:
        return None


def _collect_resource_snapshot() -> Dict[str, Any]:
    """Build the resource portion of the heartbeat payload. Any field that
    fails to sample is simply omitted — the server keeps the previously
    persisted value via COALESCE."""
    snapshot: Dict[str, Any] = {}
    load = _read_loadavg_1min()
    if load is not None:
        snapshot["cpu_load_1min"] = load
    total_mb, used_mb = _read_meminfo_mb()
    if total_mb is not None:
        snapshot["mem_total_mb"] = total_mb
    if used_mb is not None:
        snapshot["mem_used_mb"] = used_mb
    total_gb, used_gb = _read_disk_root_gb()
    if total_gb is not None:
        snapshot["disk_total_gb"] = total_gb
    if used_gb is not None:
        snapshot["disk_used_gb"] = used_gb
    cache_mb = _read_cache_size_mb()
    if cache_mb is not None:
        snapshot["cache_size_mb"] = cache_mb
    kos_version = _read_kiosk_os_version()
    if kos_version is not None:
        snapshot["kiosk_os_version"] = kos_version
    return snapshot


def send_once() -> bool:
    cfg = config.read_config()
    backend = cfg.get("BLUEBIRD_BACKEND") or ""
    slug = cfg.get("SCHOOL_SLUG") or ""
    device_id = cfg.get("DEVICE_ID") or ""
    if not (backend and slug and device_id):
        logger.info("heartbeat: skipping — device not configured yet")
        return False
    payload: Dict[str, Any] = {
        "slug": slug,
        "device_id": device_id,
        "os_version": _read_os_version(),
        "kiosk_version": __version__,
        "hostname": socket.gethostname(),
        "uptime_sec": _read_uptime(),
    }
    payload.update(_collect_resource_snapshot())
    url = backend.rstrip("/") + "/api/public/kiosk/heartbeat"
    try:
        resp = requests.post(url, json=payload, timeout=10)
    except requests.RequestException as exc:
        logger.warning("heartbeat: network error: %s", exc)
        return False
    if resp.status_code == 429:
        logger.info("heartbeat: rate limited — backing off")
        return False
    if resp.status_code >= 400:
        logger.warning("heartbeat: backend rejected status=%s body=%s", resp.status_code, resp.text[:200])
        return False
    return True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = config.read_config()
    try:
        interval = max(30, int(cfg.get("HEARTBEAT_INTERVAL_SEC") or 60))
    except ValueError:
        interval = 60
    logger.info("heartbeat: starting (interval=%ss)", interval)
    while True:
        send_once()
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main() or 0)
