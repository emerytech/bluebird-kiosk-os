"""Background heartbeat sender — POSTs once per minute to the BlueBird backend.

Run as a long-lived service via `python3 -m bluebird_kiosk.services.heartbeat`.

Each heartbeat carries:
  • Identification (slug, device id, hostname, OS + kiosk version)
  • Uptime (from /proc/uptime)
  • Resource snapshot (CPU load, memory, disk, sync cache size)

The resource fields let super-admin see at a glance which kiosks are
running hot, low on disk, or short on memory — without having to SSH
into each one or open the device admin overlay.

Remote commands (Task #29): when the device has a license token, the
heartbeat carries `Authorization: Bearer <token>` and the backend's
response may include a `commands` list the operator queued from the
fleet console. Each command maps to a FIXED systemctl argv below —
there is no free-text execution path. This service runs as the
unprivileged `bluebird-kiosk` user; the systemctl calls are authorized
by the 49-bluebird-kiosk polkit rules (manage-units + reboot), the same
grants the PIN-gated admin overlay relies on.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


_OUTPUTS_PATH = Path("/var/lib/bluebird-kiosk/outputs.json")


def _collect_outputs() -> Optional[List[Dict[str, Any]]]:
    """Read the display output inventory published by kiosk-display-manager (the heartbeat is
    sandboxed and can't reach the sway socket itself). Returns None if absent/empty so the
    server keeps the previously stored inventory via COALESCE."""
    try:
        data = json.loads(_OUTPUTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, list) or not data:
        return None
    out: List[Dict[str, Any]] = []
    for o in data[:16]:
        if isinstance(o, dict) and o.get("name"):
            out.append({
                "name": str(o["name"])[:64],
                "mode": (str(o["mode"])[:32] if o.get("mode") else None),
                "active": (bool(o["active"]) if o.get("active") is not None else None),
                "transform": (str(o["transform"])[:16] if o.get("transform") else None),
            })
    return out or None


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


# ── Remote command executor (Task #29) ───────────────────────────────────────
#
# Closed mapping: command name → what actually runs. Anything not in this
# dict is reported back as an error without executing. The backend enforces
# the same allowlist on enqueue; this is the defense-in-depth copy.

_SYSTEMCTL = "/usr/bin/systemctl"


def _run_systemctl(argv: List[str], timeout_s: int = 30) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(
            [_SYSTEMCTL, *argv],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"systemctl {' '.join(argv)} failed: {exc}"
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return False, f"rc={proc.returncode}: {out[:500]}"
    return True, out[:500] or "ok"


def _cmd_take_screenshot() -> Tuple[bool, str]:
    return _run_systemctl(["start", "bluebird-screenshot.service"], timeout_s=60)


def _cmd_force_sync() -> Tuple[bool, str]:
    return _run_systemctl(["restart", "bluebird-kiosk-sync.service"])


def _cmd_check_update() -> Tuple[bool, str]:
    # --no-block: the update can take minutes (apt churn); don't hold the
    # heartbeat loop hostage. The operator watches kiosk_os_version flip
    # on subsequent heartbeats.
    return _run_systemctl(["start", "--no-block", "bluebird-update.service"])


def _cmd_restart_kiosk() -> Tuple[bool, str]:
    # Ubuntu installs run the session through greetd (the watchdog's
    # field-proven heal path); Debian live-build images use
    # bluebird-kiosk.service. Try greetd first, fall back.
    ok, msg = _run_systemctl(["restart", "greetd"])
    if ok:
        return True, "restarted greetd: " + msg
    ok2, msg2 = _run_systemctl(["restart", "bluebird-kiosk.service"])
    if ok2:
        return True, "restarted bluebird-kiosk.service: " + msg2
    return False, f"greetd: {msg} / bluebird-kiosk.service: {msg2}"


_COMMAND_EXECUTORS = {
    "take_screenshot": _cmd_take_screenshot,
    "force_sync": _cmd_force_sync,
    "check_update": _cmd_check_update,
    "restart_kiosk": _cmd_restart_kiosk,
    # reboot is special-cased in _execute_commands: result is POSTed
    # BEFORE systemctl reboot, because afterwards there is no process
    # left to report it.
}


def _post_command_result(
    backend: str, token: str, command_id: int, *, status: str, result: str,
) -> None:
    url = backend.rstrip("/") + "/api/public/kiosk/command-result"
    try:
        requests.post(
            url,
            json={"command_id": int(command_id), "status": status, "result": result[:8000]},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        # Best-effort: the backend marks unreported 'sent' rows as
        # timeout after 10 min, so a lost result self-heals server-side.
        logger.warning("command %s: could not report result: %s", command_id, exc)


def _execute_commands(commands: List[Dict[str, Any]], backend: str, token: str) -> None:
    for entry in commands:
        try:
            cmd_id = int(entry.get("id", 0))
            name = str(entry.get("command", ""))
        except (TypeError, ValueError):
            continue
        if cmd_id <= 0:
            continue
        logger.info("command %s: executing %r", cmd_id, name)

        if name == "reboot":
            # Report first — after systemctl reboot there's nobody left
            # to phone home. If the reboot then somehow fails, the next
            # heartbeat (with intact uptime) is the operator's tell.
            _post_command_result(
                backend, token, cmd_id, status="done", result="reboot initiated",
            )
            ok, msg = _run_systemctl(["reboot"])
            if not ok:
                logger.error("command %s: reboot failed after reporting: %s", cmd_id, msg)
            return  # no point processing further commands either way

        executor = _COMMAND_EXECUTORS.get(name)
        if executor is None:
            _post_command_result(
                backend, token, cmd_id,
                status="error", result=f"unknown command: {name!r}",
            )
            continue
        try:
            ok, msg = executor()
        except Exception as exc:  # never let one command kill the loop
            ok, msg = False, f"executor crashed: {exc}"
        logger.info("command %s: %s — %s", cmd_id, "done" if ok else "error", msg[:200])
        _post_command_result(
            backend, token, cmd_id,
            status="done" if ok else "error", result=msg,
        )


# ── Display power schedule cache ──────────────────────────────────────────────
#
# The backend may include a per-tenant `power_schedule` in the heartbeat
# response. We persist it where the root power-scheduler timer
# (kiosk-power-scheduler) can read it. Written to /var/lib/bluebird-kiosk —
# owned by this (bluebird-kiosk) user — so no privileged write is needed.

_POWER_SCHEDULE_PATH = Path("/var/lib/bluebird-kiosk/power_schedule.json")


def _cache_power_schedule(schedule: Any) -> None:
    """Atomically persist the display power schedule. A null/omitted schedule
    is stored as {} (disabled), so disabling it in the CMS clears the local
    copy on the next heartbeat. Best-effort: a write failure is logged and
    never breaks the heartbeat."""
    if schedule is None:
        schedule = {}
    if not isinstance(schedule, dict):
        return
    try:
        _POWER_SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _POWER_SCHEDULE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(schedule), encoding="utf-8")
        tmp.replace(_POWER_SCHEDULE_PATH)
    except OSError as exc:
        logger.warning("heartbeat: could not cache power schedule: %s", exc)


_POWER_STATE_PATH = Path("/var/lib/bluebird-kiosk/power_state.json")


def _read_display_scheduled_off() -> bool:
    """True when the power scheduler has the panel blanked by schedule (not by
    an incident — incidents keep the display on). Reported so the fleet console
    shows 'scheduled off' instead of flagging a dark kiosk. Best-effort -> False."""
    try:
        data = json.loads(_POWER_STATE_PATH.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get("scheduled_off"))
    except (OSError, ValueError):
        return False


def _apply_display_assignment(assignment: Any) -> None:
    """Translate a Beacon `display_assignment` (bearer heartbeats only) into
    kiosk.conf so launch-kiosk-chromium picks the right URL on its next start:
      • {mode:'signage', slug, public_key} -> DISPLAY_MODE=signage + SIGNAGE_URL
      • null / non-signage              -> DISPLAY_MODE=legacy_wall (revert)

    Only when the effective mode OR url CHANGES do we rewrite the config and queue
    a kiosk restart so Chromium reloads onto the new target — we do NOT hot-swap a
    live URL, and we must NOT restart on every heartbeat. Best-effort: any failure
    is logged and never breaks the heartbeat or the emergency loopback."""
    try:
        cfg = config.read_config()
        backend = cfg.get("BLUEBIRD_BACKEND") or ""
        cur_mode = cfg.get("DISPLAY_MODE") or "legacy_wall"
        cur_url = cfg.get("SIGNAGE_URL") or ""
        if isinstance(assignment, dict) and str(assignment.get("mode") or "") == "signage":
            new_url = config.derive_beacon_url(
                backend, str(assignment.get("slug") or ""), str(assignment.get("public_key") or ""))
            new_mode = "signage" if new_url else "legacy_wall"
        else:
            new_url = ""
            new_mode = "legacy_wall"
        if new_mode == cur_mode and new_url == cur_url:
            return  # no change — never churn the config or restart
        config.write_config({"DISPLAY_MODE": new_mode, "SIGNAGE_URL": new_url})
        logger.info(
            "heartbeat: display mode %s -> %s (url=%s) — restarting kiosk",
            cur_mode, new_mode, new_url or "(none)")
        _cmd_restart_kiosk()
    except Exception as exc:  # noqa: BLE001 — heartbeat must survive any failure here
        logger.warning("heartbeat: could not apply display assignment: %s", exc)


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
        "display_scheduled_off": _read_display_scheduled_off(),
    }
    payload.update(_collect_resource_snapshot())
    outs = _collect_outputs()
    if outs:
        payload["outputs"] = outs
    url = backend.rstrip("/") + "/api/public/kiosk/heartbeat"
    # Bearer when licensed — this is what authorizes remote-command
    # delivery. Unlicensed (pre-firstboot) kiosks heartbeat slug-only
    # exactly as before.
    token = config.read_license_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
    except requests.RequestException as exc:
        logger.warning("heartbeat: network error: %s", exc)
        return False
    if resp.status_code == 429:
        logger.info("heartbeat: rate limited — backing off")
        return False
    if resp.status_code >= 400:
        logger.warning("heartbeat: backend rejected status=%s body=%s", resp.status_code, resp.text[:200])
        return False
    # Parse the response body once. It may carry a per-tenant display power
    # schedule (any heartbeat) and remote commands (bearer heartbeats only).
    try:
        data = resp.json() or {}
    except ValueError:
        data = {}
    # Only (re)write the cache when the backend actually sent the key, so an
    # older backend that omits it never clobbers a good local schedule.
    if isinstance(data, dict) and "power_schedule" in data:
        _cache_power_schedule(data.get("power_schedule"))
    # Remote commands ride the response. Only possible when we sent a
    # bearer (the backend never includes commands otherwise).
    if token:
        commands = data.get("commands") or [] if isinstance(data, dict) else []
        if commands:
            _execute_commands(commands, backend, token)
    # Beacon display assignment — bearer heartbeats only (a slug-only v1 heartbeat
    # always carries a null assignment, which would wrongly revert a signage kiosk
    # to Legacy Wall, so we never act on it). Only when the backend actually sent
    # the key, so an older backend that omits it never clobbers the local mode.
    if token and isinstance(data, dict) and "display_assignment" in data:
        _apply_display_assignment(data.get("display_assignment"))
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
