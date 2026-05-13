"""Wayland output (display) controls via `wlr-randr` and `brightnessctl`.

Only the running sway compositor session has a valid WAYLAND_DISPLAY socket,
so these calls must be made from a process running as the kiosk user with
the same XDG_RUNTIME_DIR — i.e., from the bluebird-admin service (which we
arrange to share that environment).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List


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
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    outputs: List[DisplayOutput] = []
    name = transform = "normal"
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
            elif "@" in stripped and "Hz" in stripped:
                if stripped.endswith("(current)"):
                    current = stripped.replace("(current)", "").strip()
                modes.append(stripped.split(" ")[0])
    _flush()
    return outputs


def set_rotation(output: str, transform: str) -> tuple[bool, str]:
    if transform not in {"normal", "90", "180", "270"}:
        return False, "Invalid transform."
    try:
        result = subprocess.run(
            ["/usr/bin/wlr-randr", "--output", output, "--transform", transform],
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
