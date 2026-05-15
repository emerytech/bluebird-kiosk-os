"""System-level operations: restart kiosk, reboot, shutdown, view logs, factory reset.

All operations shell out to systemctl/journalctl with fixed argv. The kiosk
user must be granted the matching polkit rules — see
build/live-build/config/includes.chroot/etc/polkit-1/rules.d/bluebird-kiosk.rules.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional


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


def _find_sway_socket() -> Optional[str]:
    """Locate the running sway IPC socket. Sway writes it to
    /run/user/<uid>/sway-ipc.<uid>.<pid>.sock on session start.

    Returns the first match or None if sway isn't running. We deliberately
    don't cache the answer — a sway restart will move the socket.
    """
    import glob
    import os as _os
    uid = _os.getuid()
    candidates = sorted(
        glob.glob(f"/run/user/{uid}/sway-ipc.{uid}.*.sock"),
        key=lambda p: _os.path.getmtime(p),
        reverse=True,
    )
    return candidates[0] if candidates else None


def reload_kiosk_display() -> tuple[bool, str]:
    """Kill the currently-running kiosk Chromium window and ask sway to
    spawn a fresh one. Recovers from a blank or otherwise-stuck slideshow
    without needing a full reboot.

    Two pieces:
      1. pkill the chromium process whose argv contains the kiosk URL.
         Targets only the kiosk window (admin overlay has a different URL).
      2. swaymsg `exec /opt/bluebird-kiosk/bin/launch-kiosk-chromium` —
         relaunch inside the sway session so the new window has a display.

    Both pieces are best-effort; failures are reported but don't roll back.
    """
    sway_sock = _find_sway_socket()
    if sway_sock is None:
        return False, "sway session not found — is the kiosk in graphical mode?"
    env = {**os.environ, "SWAYSOCK": sway_sock}

    # Step 1: kill the kiosk Chromium. Match on the URL since both kiosk and
    # admin Chromium have similar argv otherwise.
    try:
        subprocess.run(
            ["/usr/bin/pkill", "-f",
             r"chromium.*--app=http://127.0.0.1:.*legacy-wall"],
            check=False, env=env, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Step 2: re-exec the launcher inside sway. swaymsg lives in the sway
    # package which is always installed on a kiosk.
    try:
        result = subprocess.run(
            ["/usr/bin/swaymsg", "exec",
             "/opt/bluebird-kiosk/bin/launch-kiosk-chromium"],
            env=env, capture_output=True, text=True,
            check=False, timeout=5,
        )
    except FileNotFoundError as exc:
        return False, f"swaymsg missing: {exc}"
    except subprocess.TimeoutExpired:
        return False, "swaymsg timed out"
    if result.returncode != 0:
        return False, (result.stderr.strip() or "swaymsg failed")
    return True, "Kiosk display relaunched."


def open_admin_overlay() -> tuple[bool, str]:
    """Ask sway to spawn the admin overlay Chromium window. Used by the
    cloud Legacy Wall when an in-admin-mode user clicks "Kiosk Settings"
    in the settings menu — same effect as the 5-finger gesture or
    Ctrl+Alt+B. The overlay is PIN-gated by the local admin app, so
    this entry point doesn't add a new privilege — it just exposes the
    existing one to users on a touchscreen without the gesture habit."""
    sway_sock = _find_sway_socket()
    if sway_sock is None:
        return False, "sway session not found — is the kiosk in graphical mode?"
    env = {**os.environ, "SWAYSOCK": sway_sock}
    try:
        result = subprocess.run(
            ["/usr/bin/swaymsg", "exec",
             "/opt/bluebird-kiosk/bin/launch-admin-overlay"],
            env=env, capture_output=True, text=True,
            check=False, timeout=5,
        )
    except FileNotFoundError as exc:
        return False, f"swaymsg missing: {exc}"
    except subprocess.TimeoutExpired:
        return False, "swaymsg timed out"
    if result.returncode != 0:
        return False, (result.stderr.strip() or "swaymsg failed")
    return True, "Admin overlay opening."


def close_admin_overlay() -> tuple[bool, str]:
    """Kill the admin overlay Chromium window. Identified by its
    --user-data-dir which is unique to the admin Chromium instance
    (see launch-admin-overlay)."""
    try:
        result = subprocess.run(
            ["/usr/bin/pkill", "-f",
             r"chromium.*--user-data-dir=/var/lib/bluebird-kiosk/admin-chromium"],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"pkill failed: {exc}"
    # pkill returns 1 when no processes matched — that's not an error here
    # (admin overlay may already be closed).
    return True, "Admin overlay closed."


def list_diagnostics_for_console() -> list:
    """Return the cached diagnostic list (UI-shaped) for the Console tab."""
    from . import diagnostics
    return [
        {
            "id": d.id,
            "label": d.label,
            "command": d.command,
            "description": d.description,
            "sort_order": d.sort_order,
        }
        for d in diagnostics.load_cached_diagnostics()
    ]


def refresh_diagnostics_from_backend() -> dict:
    """On-demand refresh of the cached diagnostic list. Called by the admin
    overlay's Console tab so staff can pick up server-side edits without
    waiting for the next 6-hour sync tick."""
    from . import diagnostics
    from .. import config
    cfg = config.read_config()
    backend = cfg.get("BLUEBIRD_BACKEND") or ""
    try:
        token = (Path("/etc/bluebird/license.token").read_text(encoding="utf-8")
                 .strip())
    except OSError:
        token = ""
    result = diagnostics.refresh_from_server(backend, token)
    if result is None:
        return {"ok": False, "count": len(diagnostics.load_cached_diagnostics())}
    return {"ok": True, "count": len(result)}


def run_diagnostic(diag_id: int, timeout_s: int = 30) -> dict:
    """Execute one entry from the cached super-admin diagnostic allow-list.

    Two gates protect this surface:
      1. The request body only carries `diag_id` (an integer). The
         command itself is read from the locally-cached allow-list. So a
         forged request can only invoke commands that are already on the
         allow-list — never arbitrary strings.
      2. The cached command is re-checked against the regex guard
         immediately before exec, so a row that slipped past a buggy or
         outdated server-side guard still won't run.

    Runs as the unprivileged `bluebird-kiosk` user (sudo not granted).
    30-second timeout, output capped at 64 KB, every execution logged
    to the journal under tag `bluebird-admin-shell`.
    """
    from . import diagnostics  # late import to avoid module cycle

    diag = diagnostics.get_diagnostic_by_id(int(diag_id))
    if diag is None:
        return {
            "ok": False,
            "error": "not_in_allow_list",
            "label": "",
            "cmd": "",
            "stdout": "",
            "stderr": (
                f"Diagnostic #{diag_id} is not in the locally-cached "
                "allow-list. Refresh from the Console tab or wait for the "
                "next sync tick."
            ),
            "exit_code": 126,
            "timed_out": False,
        }
    cmd = diag.command.strip()
    guard = diagnostics.is_destructive(cmd)
    if guard is not None:
        return {
            "ok": False,
            "error": "destructive_pattern",
            "label": diag.label,
            "cmd": cmd,
            "stdout": "",
            "stderr": (
                f"Refusing to execute: the cached command matches the "
                f"destructive-pattern guard ({guard}). The server should "
                "have rejected this on save."
            ),
            "exit_code": 126,
            "timed_out": False,
        }
    if not cmd:
        return {
            "ok": False, "error": "empty_command", "label": diag.label,
            "cmd": "", "stdout": "", "stderr": "(empty command)",
            "exit_code": 0, "timed_out": False,
        }

    try:
        subprocess.run(
            ["/usr/bin/logger", "-t", "bluebird-admin-shell",
             f"diag_id={diag.id} label={diag.label!r} cmd={cmd!r}"],
            check=False, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        result = subprocess.run(
            ["/bin/bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=max(1, min(120, int(timeout_s))),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": True, "label": diag.label, "cmd": cmd,
            "stdout": (exc.stdout.decode("utf-8", "replace") if exc.stdout else ""),
            "stderr": (exc.stderr.decode("utf-8", "replace") if exc.stderr else "")
                + f"\n[command timed out after {timeout_s}s]",
            "exit_code": 124, "timed_out": True,
        }
    except FileNotFoundError as exc:
        return {
            "ok": True, "label": diag.label, "cmd": cmd,
            "stdout": "", "stderr": f"shell missing: {exc}",
            "exit_code": 127, "timed_out": False,
        }

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    LIMIT = 64 * 1024
    if len(stdout) > LIMIT:
        stdout = stdout[:LIMIT] + f"\n[…{len(stdout) - LIMIT} bytes truncated]"
    if len(stderr) > LIMIT:
        stderr = stderr[:LIMIT] + f"\n[…{len(stderr) - LIMIT} bytes truncated]"
    return {
        "ok": True, "label": diag.label, "cmd": cmd,
        "stdout": stdout, "stderr": stderr,
        "exit_code": int(result.returncode), "timed_out": False,
    }


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
