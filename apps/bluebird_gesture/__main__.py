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

import json
import logging
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Dict, Tuple

import evdev
from evdev import ecodes


HOLD_FINGERS = 5
HOLD_SECONDS = 2.0
COOLDOWN_SECONDS = 5.0
ADMIN_URL = os.environ.get("BLUEBIRD_ADMIN_URL", "http://127.0.0.1:7311/admin")

# Touch-to-wake: a tap while the panel is asleep on schedule grants a temporary reprieve.
# We record an override the power scheduler honors, and (when the panel is dark) kick the
# scheduler for an instant wake instead of waiting up to its 60s tick.
STATE_PATH = Path("/var/lib/bluebird-kiosk/power_state.json")
WAKE_PATH = Path("/var/lib/bluebird-kiosk/wake_override.json")
WAKE_SECONDS = 30 * 60        # reprieve length (matches the scheduler's check window)
WAKE_DEBOUNCE = 3.0           # don't re-write the override more than this often (drag-friendly)

logger = logging.getLogger("bluebird-gesture")


def _power_state() -> Tuple[bool, str]:
    """(display_on, reason) from the scheduler's last decision. Unknown => (True, '') so we
    never try to wake a panel we can't confirm is off."""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
        return bool(data.get("display_on", True)), str(data.get("reason", ""))
    except (OSError, ValueError, TypeError):
        return True, ""


def _wake_on_touch() -> None:
    """A screen tap re-arms the temporary wake. No-op unless the panel is asleep on schedule
    (wake it) or already in a wake window (extend it) — so normal on-hours browsing never
    writes. The scheduler (which can reach the sway socket) does the actual DPMS toggle."""
    on, reason = _power_state()
    if not ((not on and reason == "schedule") or reason == "wake_override"):
        return
    try:
        WAKE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = WAKE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"wake_until": time.time() + WAKE_SECONDS}), encoding="utf-8")
        tmp.replace(WAKE_PATH)
    except OSError as exc:
        logger.warning("could not write wake override: %s", exc)
        return
    if not on:   # panel is dark — kick the scheduler for an instant wake (else ≤60s latency)
        try:
            subprocess.run(
                ["/usr/bin/systemctl", "start", "bluebird-power-scheduler.service"],
                check=False, capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("could not trigger power scheduler: %s", exc)
        logger.info("touch-to-wake: armed %ds + triggered wake", WAKE_SECONDS)


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
    last_wake = 0.0

    while True:
        r, _, _ = select.select([dev.fd for dev in devices], [], [], 0.25)
        touch_down = False
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
                                touch_down = True   # a finger landed (even a quick tap)
                except (BlockingIOError, OSError):
                    continue

        active = sum(len(v) for v in slots.values())
        now = time.monotonic()

        # Touch-to-wake: any finger-down re-arms the wake window. Catches a quick tap (the
        # down event is seen even if the finger lifts in the same read batch); debounced so a
        # drag / multi-touch doesn't spam. _wake_on_touch() self-gates to off-hours.
        if touch_down and now - last_wake >= WAKE_DEBOUNCE:
            _wake_on_touch()
            last_wake = now

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
