"""Touch admin-gesture daemon for BlueBird Kiosk OS.

Watches every touchscreen `/dev/input/event*` device via python-evdev and opens the
PIN-gated admin overlay on either of two gestures:
- **5-finger long-press** (>=5 slots for >=2s) — the original; needs a panel that reports
  5 simultaneous touches.
- **corner-tap** (tap any screen corner 5x within 4s) — reliable on any panel, including
  2-touch panels, and rotation-agnostic.

Launch path: this daemon is a sandboxed system service with no Wayland session, and the kiosk
chromium runs fullscreen (sway hides every other window behind it). So we DON'T spawn chromium
ourselves — we ask the running sway to exec the shared `launch-admin-overlay` script, which
unfullscreens the kiosk first and starts chromium with the right flags (identical to the
Ctrl+Alt+B keybinding). Reaching sway's IPC socket needs /run/user visible — hence the unit
drops ProtectHome.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import pwd
import select
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Tuple

import evdev
from evdev import ecodes


HOLD_FINGERS = 5
HOLD_SECONDS = 2.0
COOLDOWN_SECONDS = 5.0
ADMIN_URL = os.environ.get("BLUEBIRD_ADMIN_URL", "http://127.0.0.1:7311/admin")
KIOSK_USER = "bluebird-kiosk"
OVERLAY_LAUNCHER = "/opt/bluebird-kiosk/bin/launch-admin-overlay"

# Corner-tap admin gesture — reliable on ANY panel, unlike the 5-finger hold (which needs a
# panel that reports 5 simultaneous touches and is awkward to perform). Tap any screen corner
# CORNER_TAPS times within CORNER_WINDOW seconds. "Any corner" keeps it rotation-agnostic
# (rotating the display just permutes which physical corner is which). CORNER_FRAC = how close
# to a corner (fraction of each axis) still counts.
CORNER_TAPS = 5
CORNER_WINDOW = 4.0
CORNER_FRAC = 0.15

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


def _sway_socket() -> "str | None":
    """The kiosk user's sway IPC socket. This daemon runs as root, so we look up the kiosk
    user's uid (not our own); root can connect to that socket directly — no sudo needed."""
    try:
        uid = pwd.getpwnam(KIOSK_USER).pw_uid
    except KeyError:
        return None
    socks = sorted(glob.glob("/run/user/%d/sway-ipc.%d.*.sock" % (uid, uid)))
    return socks[0] if socks else None


def _is_corner(x, y, xmax, ymax, frac: float = CORNER_FRAC) -> bool:
    """True if (x, y) is within `frac` of ANY of the four corners. Any-corner so a rotated
    display still works. False on unknown ranges/coords (so it never fires by accident)."""
    if not xmax or not ymax or x is None or y is None:
        return False
    nx, ny = x / xmax, y / ymax
    return (nx < frac or nx > 1 - frac) and (ny < frac or ny > 1 - frac)


def _xy_range(dev) -> Tuple[int, int]:
    """(x_max, y_max) for a touch device, preferring the multitouch axes. (0, 0) if unknown —
    which disables corner detection for that device (the 5-finger gesture still works)."""
    caps = dict(dev.capabilities().get(ecodes.EV_ABS, []))

    def _max(*codes):
        for c in codes:
            info = caps.get(c)
            if info is not None and getattr(info, "max", 0):
                return info.max
        return 0

    return (_max(ecodes.ABS_MT_POSITION_X, ecodes.ABS_X),
            _max(ecodes.ABS_MT_POSITION_Y, ecodes.ABS_Y))


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
    """Open the PIN-gated admin overlay. We don't spawn chromium directly — this daemon has no
    Wayland session and the kiosk chromium is fullscreen (a directly-spawned window opens HIDDEN
    behind it; that's the long-standing reason the 5-finger gesture "did nothing"). Instead we
    tell the running sway to exec the shared launcher, which unfullscreens the kiosk first and
    starts chromium correctly — exactly what Ctrl+Alt+B does."""
    sock = _sway_socket()
    if not sock:
        logger.warning("admin overlay: no sway socket found — session not up yet?")
        return
    logger.info("admin overlay: launching via sway exec")
    try:
        subprocess.run(
            ["/usr/bin/swaymsg", "-s", sock, "exec", OVERLAY_LAUNCHER],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("admin overlay launch failed: %s", exc)


def watch(devices: list) -> None:
    slots: Dict[int, Dict[int, bool]] = {dev.fd: {} for dev in devices}
    current_slot: Dict[int, int] = {dev.fd: 0 for dev in devices}
    pos: Dict[int, Dict[int, list]] = {dev.fd: {} for dev in devices}   # slot -> [x, y]
    ranges = {dev.fd: _xy_range(dev) for dev in devices}
    held_since: float | None = None
    last_trigger = 0.0
    last_wake = 0.0
    corner_taps: List[float] = []

    while True:
        r, _, _ = select.select([dev.fd for dev in devices], [], [], 0.25)
        touch_down = False
        lifts: List[Tuple[int, list]] = []        # (fd, [x, y]) for fingers lifted this batch
        for dev in devices:
            if dev.fd in r:
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_ABS:
                            continue
                        if event.code == ecodes.ABS_MT_SLOT:
                            current_slot[dev.fd] = event.value
                        elif event.code == ecodes.ABS_MT_POSITION_X:
                            pos[dev.fd].setdefault(current_slot[dev.fd], [None, None])[0] = event.value
                        elif event.code == ecodes.ABS_MT_POSITION_Y:
                            pos[dev.fd].setdefault(current_slot[dev.fd], [None, None])[1] = event.value
                        elif event.code == ecodes.ABS_MT_TRACKING_ID:
                            slot = current_slot[dev.fd]
                            if event.value == -1:
                                xy = pos[dev.fd].get(slot)   # capture lift position (now known)
                                if xy is not None:
                                    lifts.append((dev.fd, list(xy)))
                                slots[dev.fd].pop(slot, None)
                                pos[dev.fd].pop(slot, None)
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

        # Corner-tap admin gesture: a tap that lifts in any corner counts; CORNER_TAPS of them
        # within CORNER_WINDOW opens admin. (Position is read at lift, when it's known.)
        for fd, xy in lifts:
            xmax, ymax = ranges.get(fd, (0, 0))
            if _is_corner(xy[0], xy[1], xmax, ymax):
                corner_taps.append(now)
        if corner_taps:
            corner_taps = [t for t in corner_taps if now - t <= CORNER_WINDOW]
            if len(corner_taps) >= CORNER_TAPS and now - last_trigger >= COOLDOWN_SECONDS:
                logger.info("corner-tap admin gesture (%d taps)", len(corner_taps))
                _spawn_admin_overlay()
                last_trigger = now
                corner_taps = []

        # 5-finger long-press (now launched via the proper unfullscreen path too).
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
