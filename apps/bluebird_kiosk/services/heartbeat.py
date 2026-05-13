"""Background heartbeat sender — POSTs once per minute to the BlueBird backend.

Run as a long-lived service via `python3 -m bluebird_kiosk.services.heartbeat`.
"""
from __future__ import annotations

import logging
import socket
import sys
import time

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


def send_once() -> bool:
    cfg = config.read_config()
    backend = cfg.get("BLUEBIRD_BACKEND") or ""
    slug = cfg.get("SCHOOL_SLUG") or ""
    device_id = cfg.get("DEVICE_ID") or ""
    if not (backend and slug and device_id):
        logger.info("heartbeat: skipping — device not configured yet")
        return False
    payload = {
        "slug": slug,
        "device_id": device_id,
        "os_version": _read_os_version(),
        "kiosk_version": __version__,
        "hostname": socket.gethostname(),
        "uptime_sec": _read_uptime(),
    }
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
