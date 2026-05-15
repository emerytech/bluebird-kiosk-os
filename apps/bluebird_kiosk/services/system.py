"""System-level operations: restart kiosk, reboot, shutdown, view logs, factory reset.

All operations shell out to systemctl/journalctl with fixed argv. The kiosk
user must be granted the matching polkit rules — see
build/live-build/config/includes.chroot/etc/polkit-1/rules.d/bluebird-kiosk.rules.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List


def restart_kiosk() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "restart", "bluebird-kiosk.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"systemctl failed: {exc}"
    if result.returncode != 0:
        return False, (result.stderr.strip() or "restart failed")
    return True, "Kiosk restarted."


def reboot() -> tuple[bool, str]:
    try:
        subprocess.Popen(["/usr/bin/systemctl", "reboot"])
    except FileNotFoundError as exc:
        return False, f"systemctl missing: {exc}"
    return True, "Rebooting…"


def shutdown() -> tuple[bool, str]:
    try:
        subprocess.Popen(["/usr/bin/systemctl", "poweroff"])
    except FileNotFoundError as exc:
        return False, f"systemctl missing: {exc}"
    return True, "Shutting down…"


def recent_logs(lines: int = 200) -> str:
    lines = max(20, min(2000, int(lines)))
    try:
        result = subprocess.run(
            [
                "/usr/bin/journalctl",
                "-u", "bluebird-kiosk.service",
                "-u", "bluebird-admin.service",
                "-u", "bluebird-gesture.service",
                "-u", "bluebird-heartbeat.service",
                "-n", str(lines),
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"journalctl failed: {exc}"
    return result.stdout or result.stderr or ""


def start_update() -> tuple[bool, str]:
    """Kick off a `bluebird-update.service` run in the background.

    The service is a oneshot unit that re-fetches the install bootstrap from
    bluebird-alerts.com and re-runs install.sh — same as the manual
    `curl … | sudo bash` workflow, but routed through a systemd unit so we
    can drive it from the unprivileged admin app via polkit-allowed
    `systemctl start`.

    Returns immediately (the unit launches detached); the admin UI polls
    `update_status()` to follow progress.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "start", "--no-block", "bluebird-update.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"systemctl failed: {exc}"
    if result.returncode != 0:
        return False, (result.stderr.strip() or "could not start update unit")
    return True, "Update started — polling for status."


def update_status(log_lines: int = 80) -> dict:
    """Snapshot the update service's current state for the admin UI.

    Returns:
        {
          "state": "active" | "inactive" | "failed" | "activating" | "unknown",
          "log": str,   # last `log_lines` of journal output from bluebird-update
        }
    """
    try:
        active = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "bluebird-update.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"state": "unknown", "log": "systemctl unavailable"}

    state = (active.stdout or active.stderr).strip() or "unknown"

    try:
        logs = subprocess.run(
            [
                "/usr/bin/journalctl",
                "-u", "bluebird-update.service",
                "-n", str(max(20, min(500, int(log_lines)))),
                "--no-pager",
                "-o", "cat",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"state": state, "log": f"journalctl failed: {exc}"}

    return {"state": state, "log": logs.stdout or ""}


def factory_reset() -> tuple[bool, str]:
    """Drop the device back into first-boot state without reflashing.

    Removes the configured flag, the slug, the admin PIN, and the device_id.
    The next boot will run the first-boot wizard again.
    """
    paths_to_clear: List[Path] = [
        Path("/etc/bluebird/configured"),
        Path("/etc/bluebird/admin.pin"),
    ]
    for p in paths_to_clear:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            return False, f"Could not remove {p}: {exc}"

    from .. import config
    config.write_config({"SCHOOL_SLUG": "", "LEGACY_WALL_URL": "", "DEVICE_ID": ""})

    return True, "Factory reset complete. Rebooting…"
