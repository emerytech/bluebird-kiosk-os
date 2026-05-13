"""5-finger long-press detector for BlueBird Kiosk OS.

Watches every touchscreen `/dev/input/event*` device via python-evdev. When
≥5 simultaneous touch slots are active for ≥2 seconds, signals the admin
overlay by spawning a Chromium window pointed at http://127.0.0.1:7311/admin.

Why this design:
- libinput's high-level gesture API is targeted at touchpads, not touchscreens.
  Direct multi-touch slot tracking via evdev is more portable across panels.
- A single hold gesture, no swipe / corner-tap, is the simplest possible
  affordance for non-technical staff and impossible to discover by accident.
"""
from __future__ import annotations

import logging
import os
import select
import subprocess
import time
from typing import Dict

import evdev
from evdev import ecodes


HOLD_FINGERS = 5
HOLD_SECONDS = 2.0
COOLDOWN_SECONDS = 5.0
ADMIN_URL = os.environ.get("BLUEBIRD_ADMIN_URL", "http://127.0.0.1:7311/admin")

logger = logging.getLogger("bluebird-gesture")


def _is_touchscreen(dev: evdev.InputDevice) -> bool:
    caps = dev.capabilities()
    abs_caps = caps.get(ecodes.EV_ABS, [])
    abs_codes = {c[0] if isinstance(c, tuple) else c for c in abs_caps}
    return ecodes.ABS_MT_SLOT in abs_codes and ecodes.ABS_MT_TRACKING_ID in abs_codes


def _open_touchscreens() -> list:
    devs = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except (OSError, PermissionError):
            continue
        if _is_touchscreen(dev):
            logger.info("watching %s (%s)", path, dev.name)
            devs.append(dev)
    return devs


def _spawn_admin_overlay() -> None:
    logger.info("gesture: launching admin overlay")
    try:
        subprocess.Popen(
            [
                "/usr/bin/chromium",
                "--app=" + ADMIN_URL,
                "--no-first-run",
                "--noerrdialogs",
                "--password-store=basic",
                "--user-data-dir=/var/lib/bluebird-kiosk/admin-chromium",
                "--window-size=800,1000",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        logger.warning("admin overlay launch failed: %s", exc)


def watch(devices: list) -> None:
    slots: Dict[int, Dict[int, bool]] = {dev.fd: {} for dev in devices}
    current_slot: Dict[int, int] = {dev.fd: 0 for dev in devices}
    held_since: float | None = None
    last_trigger = 0.0

    while True:
        r, _, _ = select.select([dev.fd for dev in devices], [], [], 0.25)
        for dev in devices:
            if dev.fd in r:
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_ABS:
                            continue
                        if event.code == ecodes.ABS_MT_SLOT:
                            current_slot[dev.fd] = event.value
                        elif event.code == ecodes.ABS_MT_TRACKING_ID:
                            slot = current_slot[dev.fd]
                            if event.value == -1:
                                slots[dev.fd].pop(slot, None)
                            else:
                                slots[dev.fd][slot] = True
                except (BlockingIOError, OSError):
                    continue

        active = sum(len(v) for v in slots.values())
        now = time.monotonic()

        if active >= HOLD_FINGERS:
            if held_since is None:
                held_since = now
            elif now - held_since >= HOLD_SECONDS and now - last_trigger >= COOLDOWN_SECONDS:
                _spawn_admin_overlay()
                last_trigger = now
                held_since = None
        else:
            held_since = None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    while True:
        devices = _open_touchscreens()
        if not devices:
            logger.info("no touchscreens detected, retry in 30s")
            time.sleep(30)
            continue
        try:
            watch(devices)
        except Exception as exc:
            logger.warning("watch loop error: %s — restarting in 5s", exc)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
