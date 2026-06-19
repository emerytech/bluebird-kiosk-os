"""Wayland output (display) controls via `wlr-randr` and `brightnessctl`.

Only the running sway compositor session exposes a valid Wayland socket,
so wlr-randr needs WAYLAND_DISPLAY + XDG_RUNTIME_DIR set to point at that
socket. The bluebird-admin systemd service runs as the bluebird-kiosk
user but doesn't get those env vars automatically (systemd system units
don't inherit user-session XDG vars). We build the env at call time:
XDG_RUNTIME_DIR=/run/user/<our-uid> + WAYLAND_DISPLAY=wayland-1 (sway's
default socket name when nothing else is running).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# Confirmed display settings (rotation/mode per output + brightness) are snapshotted here so
# they survive a reboot or an unattended self-update — kiosk-display-manager re-applies them on
# session start + hotplug. Written only on an operator CONFIRM (kept change), never on the
# auto-revert path, so an unconfirmed-and-rebooted change doesn't stick.
SETTINGS_PATH = Path("/var/lib/bluebird-kiosk/display-settings.json")


def _wayland_env() -> Dict[str, str]:
    """Return env dict with the bluebird-kiosk Wayland socket vars set.

    Probes /run/user/<uid>/wayland-* to pick the socket sway actually
    opened (usually wayland-1; falls back to wayland-0 if that's what
    we find). Returning a fresh dict, not in-place modification, so
    callers can pass it to subprocess.run without polluting our own
    environment."""
    uid = os.getuid()
    runtime = f"/run/user/{uid}"
    socks = sorted(glob.glob(f"{runtime}/wayland-*"))
    # Filter out the .lock files glob also picks up.
    socks = [s for s in socks if not s.endswith(".lock")]
    display_name = os.path.basename(socks[0]) if socks else "wayland-1"
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = runtime
    env["WAYLAND_DISPLAY"] = display_name
    return env


@dataclass(frozen=True)
class DisplayOutput:
    name: str
    enabled: bool
    current_mode: str
    available_modes: List[str]
    transform: str       # "normal" | "90" | "180" | "270" | "flipped" | ...


def list_outputs() -> List[DisplayOutput]:
    try:
        result = subprocess.run(
            ["/usr/bin/wlr-randr"],
            env=_wayland_env(),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    outputs: List[DisplayOutput] = []
    name = ""
    transform = "normal"
    current = ""
    modes: List[str] = []
    enabled = True

    def _flush() -> None:
        if name:
            outputs.append(
                DisplayOutput(
                    name=name,
                    enabled=enabled,
                    current_mode=current,
                    available_modes=list(modes),
                    transform=transform,
                )
            )

    # wlr-randr output format (one block per output):
    #   DP-1 "Dell ... (DP-1)"
    #     Make: Dell Inc.
    #     ...
    #     Enabled: yes
    #     Modes:
    #       1920x1080 px, 60.000000 Hz (preferred, current)
    #       1600x900 px, 60.000000 Hz
    #     Position: 0,0
    #     Transform: normal
    for line in result.stdout.splitlines():
        if line and not line.startswith(" "):
            _flush()
            name = line.split(" ")[0].strip()
            enabled = True
            current = ""
            modes = []
            transform = "normal"
        else:
            stripped = line.strip()
            if stripped.startswith("Enabled:"):
                enabled = "yes" in stripped.lower()
            elif stripped.startswith("Transform:"):
                transform = stripped.split(":", 1)[1].strip()
            elif " px," in stripped and "Hz" in stripped:
                # "1920x1080 px, 60.000000 Hz (preferred, current)"
                # Normalize to "1920x1080@60Hz" — the format wlr-randr's
                # `--mode` flag accepts when setting modes later.
                res_part, hz_part = stripped.split(" px,", 1)
                hz = hz_part.strip().split(" ")[0]
                try:
                    hz_int = int(round(float(hz)))
                except ValueError:
                    hz_int = 0
                mode_str = f"{res_part.strip()}@{hz_int}Hz" if hz_int else res_part.strip()
                if "current" in stripped:
                    current = mode_str
                modes.append(mode_str)
    _flush()
    return outputs


def set_rotation(output: str, transform: str) -> tuple[bool, str]:
    if transform not in {"normal", "90", "180", "270"}:
        return False, "Invalid transform."
    try:
        result = subprocess.run(
            ["/usr/bin/wlr-randr", "--output", output, "--transform", transform],
            env=_wayland_env(),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"wlr-randr failed: {exc}"
    if result.returncode != 0:
        return False, (result.stderr.strip() or "rotation failed")
    return True, "OK"


def set_mode(output: str, mode: str) -> tuple[bool, str]:
    if not mode or len(mode) > 32:
        return False, "Invalid mode."
    try:
        result = subprocess.run(
            ["/usr/bin/wlr-randr", "--output", output, "--mode", mode],
            env=_wayland_env(),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"wlr-randr failed: {exc}"
    if result.returncode != 0:
        return False, (result.stderr.strip() or "mode failed")
    return True, "OK"


def set_brightness(percent: int) -> tuple[bool, str]:
    percent = max(5, min(100, int(percent)))
    try:
        result = subprocess.run(
            ["/usr/bin/brightnessctl", "set", f"{percent}%"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"brightnessctl failed: {exc}"
    if result.returncode != 0:
        return False, (result.stderr.strip() or "brightness failed")
    return True, "OK"


def get_brightness() -> Optional[int]:
    """Current backlight as a 0-100 percent, or None if there's no backlight device
    (e.g. a desktop box driving an external monitor — brightnessctl has nothing to read)."""
    try:
        cur = subprocess.run(["/usr/bin/brightnessctl", "get"],
                             capture_output=True, text=True, timeout=5)
        mx = subprocess.run(["/usr/bin/brightnessctl", "max"],
                            capture_output=True, text=True, timeout=5)
        c, m = int(cur.stdout.strip()), int(mx.stdout.strip())
        return max(1, round(c * 100 / m)) if m else None
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None


def persist_settings() -> tuple[bool, str]:
    """Snapshot the CURRENT display config (per-output transform + mode, plus brightness) to
    SETTINGS_PATH so it survives reboot/update. Call this only after a change is accepted
    (operator confirm / brightness set) — never on the auto-revert path."""
    data: Dict[str, object] = {"outputs": {}, "brightness": get_brightness()}
    outs = data["outputs"]
    assert isinstance(outs, dict)
    for o in list_outputs():
        if o.enabled and o.name:
            outs[o.name] = {"transform": o.transform, "mode": o.current_mode}
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)
    except OSError as exc:
        return False, f"persist failed: {exc}"
    return True, "OK"
