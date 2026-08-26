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
import re
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


def _collect_compositor_count() -> Optional[int]:
    """Number of running sway instances.

    Exactly one is healthy. Two means a second launcher (greetd alongside
    bluebird-kiosk.service) started its own session and the pair are fighting
    over DRM master — no frames reach any output while everything else on the
    box keeps looking healthy. That state took the NEN lobby kiosk down for a
    day on 2026-08-05/06 and was invisible to the fleet console the whole time,
    because this process is a separate unit and kept reporting normally.

    Zero means no compositor at all (a box sitting at a text console). Returns
    None if pgrep is unavailable, so the server can distinguish "not reported"
    from a real count.
    """
    try:
        proc = subprocess.run(
            ["pgrep", "-x", "sway"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # pgrep exits 1 with no output when nothing matches — that is a real zero,
    # not an error.
    if proc.returncode not in (0, 1):
        return None
    return len([ln for ln in proc.stdout.split("\n") if ln.strip()])


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


_OUTPUT_HEALTH_PATH = Path("/var/lib/bluebird-kiosk/output-health.json")


def _collect_output_health() -> Optional[Dict[str, Any]]:
    """Read per-output render health published by launch-kiosk-independent: which output (if any) is
    parked on a server/browser error page (`wedged`), how many times we've auto-reloaded it
    (`reloads`), and the current window title. None when absent/empty so the server keeps the prior
    value via COALESCE. The heartbeat is sandboxed off the sway socket, so the in-session reconcile
    loop is what writes this file (same pattern as outputs.json)."""
    try:
        data = json.loads(_OUTPUT_HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    outs = data.get("outputs") if isinstance(data, dict) else None
    if not isinstance(outs, dict) or not outs:
        return None
    clean: Dict[str, Any] = {}
    for name, h in list(outs.items())[:16]:
        if not isinstance(h, dict) or not name:
            continue
        try:
            reloads = int(h.get("reloads") or 0)
        except (TypeError, ValueError):
            reloads = 0
        clean[str(name)[:64]] = {
            "wedged": bool(h.get("wedged")),
            "reloads": reloads,
            "title": (str(h.get("title"))[:120] if h.get("title") else None),
        }
    return clean or None


_WATCHDOG_STATE_PATH = Path("/run/bluebird-kiosk/watchdog.json")


def _collect_watchdog_state() -> Optional[Dict[str, Any]]:
    """Read the reliability watchdog's current state (alive/degraded/recovering/cooldown + the
    per-check booleans incl. page_fresh) so the fleet console can badge a self-healing kiosk. The
    watchdog already mirrors this to /run; we just forward it. None when absent/unparseable."""
    try:
        data = json.loads(_WATCHDOG_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    state = str(data.get("state") or "")[:32]
    if not state:
        return None
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    try:
        fail_streak = int(data.get("fail_streak") or 0)
    except (TypeError, ValueError):
        fail_streak = 0
    return {
        "state": state,
        "fail_streak": fail_streak,
        "checks": {str(k)[:32]: bool(v) for k, v in list(checks.items())[:12]},
        "last_action": (str(data.get("last_action"))[:48] if data.get("last_action") else None),
    }


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


_ACCESS_REQUEST = Path("/var/lib/bluebird-kiosk/access-reset.request")

# A crypt(3) SHA-512 record, as produced by `openssl passwd -6` / crypt.crypt
# with METHOD_SHA512 and as stored in /etc/shadow: $6$<salt>$<checksum>.
_SHADOW_RE = re.compile(r"^\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}$")


def _cmd_reset_access(args: Dict[str, Any]) -> Tuple[bool, str]:
    """Apply an operator-chosen login password sent from the fleet console.

    Recovery path for a box whose password has been lost: without it the only
    option is a reinstall, which wipes the device's display settings and media
    cache and takes a live board down.

    The console never sends a plaintext password. The server hashes it the
    moment it is submitted and ships only the crypt(3) record, which we hand to
    `chpasswd -e` — so the password itself exists nowhere on this device, in the
    command queue, or in any backup of it.

    We cannot apply it here: this process runs as bluebird-kiosk with
    NoNewPrivileges=yes and cannot change a credential. We stage the hash in a
    0600 request file and let bluebird-reset-access.service (root, oneshot) do
    the work, then remove the file whether or not the unit succeeded.
    """
    shadow = str((args or {}).get("shadow") or "").strip()
    if not shadow:
        return False, "no password supplied"
    # Validate the shape before staging it. This string is written to a file
    # that a root unit feeds to chpasswd; anything that is not a crypt record
    # has no business getting that far.
    if not _SHADOW_RE.match(shadow):
        return False, "password hash is not a valid SHA-512 crypt record"

    try:
        _ACCESS_REQUEST.write_text(json.dumps({"shadow": shadow}), encoding="utf-8")
        _ACCESS_REQUEST.chmod(0o600)
    except OSError as exc:
        return False, "could not stage the request: {0}".format(exc)

    try:
        ok, msg = _run_systemctl(["start", "bluebird-reset-access.service"], timeout_s=60)
    finally:
        # Never leave credential material staged, even if the unit failed or
        # systemctl raised.
        try:
            _ACCESS_REQUEST.unlink()
        except OSError as exc:
            logger.error("reset_access: could not remove %s: %s", _ACCESS_REQUEST, exc)

    if not ok:
        return False, "could not run the reset unit: " + msg
    return True, "password updated for the bluebird account"


_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,31}$")
# e.g. 1920x1080@60Hz — the format list_outputs()/wlr-randr use.
_MODE_RE = re.compile(r"^\d{3,5}x\d{3,5}(@\d{1,3}(\.\d+)?Hz)?$")


_DISPLAY_MODE_REQUEST = Path("/var/lib/bluebird-kiosk/display-mode.request")


def _cmd_set_display_mode(args: Dict[str, Any]) -> Tuple[bool, str]:
    """Set an output's resolution from the fleet console, and make it stick.

    Why this exists: sway picks a mode from EDID, and a sink that advertises a mode it
    cannot actually lock (the NEN lobby LG offers 4096x2160 DCI-4K five times and flags NO
    preferred mode) leaves the kiosk confidently driving an output that produces no picture
    — "No Signal" on the panel while every fleet-console health signal reads green.
    Recovering that used to require standing at the panel or SSH; it cost two days on
    2026-08-06/07.

    THIS PROCESS CANNOT TALK TO SWAY. bluebird-heartbeat.service runs as bluebird-kiosk with
    ProtectSystem=strict + ProtectHome=yes, so the wayland socket is out of reach — the same
    reason the output inventory is a file we read rather than a live query. A first cut
    called display.list_outputs() here and failed every time with "unknown output" from
    inside the sandbox. So: validate the output name against the published inventory, stage
    the request, and let bluebird-set-display-mode.service (which runs outside the sandbox
    and drops to bluebird-kiosk with the wayland env) do the apply — the same handoff shape
    reset_access uses for chpasswd.

    The MODE is validated by that unit against what wlr-randr actually advertises, because
    only it can see the real mode list. Setting an unsupported mode blanks a screen with
    nobody on site to undo it.
    """
    output = str((args or {}).get("output") or "").strip()
    mode = str((args or {}).get("mode") or "").strip()
    if not output or not mode:
        return False, "output and mode are required"
    if not _OUTPUT_NAME_RE.match(output):
        return False, "invalid output name"
    if not _MODE_RE.match(mode):
        return False, "invalid mode format (expected e.g. 1920x1080@60Hz)"

    # Output NAME check against the inventory the display manager publishes for us. We
    # cannot check the mode here — that needs the live wlr-randr list, which is why the
    # applier unit re-checks it.
    known = _collect_outputs()
    if known is None:
        return False, ("no display inventory yet — the kiosk publishes it from inside the "
                       "session, so give it one reconcile cycle after boot")
    names = [str(o.get("name") or "") for o in known]
    if output not in names:
        return False, "unknown output {0!r} (have: {1})".format(output, ", ".join(n for n in names if n) or "none")

    try:
        _DISPLAY_MODE_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        _DISPLAY_MODE_REQUEST.write_text(json.dumps({"output": output, "mode": mode}),
                                         encoding="utf-8")
    except OSError as exc:
        return False, "could not stage the request: {0}".format(exc)

    ok, msg = _run_systemctl(["start", "bluebird-set-display-mode.service"], timeout_s=45)
    if not ok:
        try:
            _DISPLAY_MODE_REQUEST.unlink()
        except OSError:
            pass
        return False, "could not run the apply unit: " + msg
    # The unit removes the request itself on both paths; a leftover means it never ran.
    if _DISPLAY_MODE_REQUEST.exists():
        try:
            _DISPLAY_MODE_REQUEST.unlink()
        except OSError:
            pass
        return False, "apply unit did not consume the request"
    return True, "{0} set to {1} and persisted".format(output, mode)


def _cmd_restart_kiosk() -> Tuple[bool, str]:
    """Restart whichever unit actually owns the session — the one that is RUNNING.

    This used to try greetd first and fall back. That is backwards on a configured box:
    bluebird-kiosk.service owns the session there (greetd is condition-skipped once
    /etc/bluebird/configured exists), so restarting greetd started a SECOND compositor
    alongside the running one. Two sway instances then fought over DRM master and no frames
    reached any output — the exact fault this button exists to clear. Field-confirmed during
    the 2026-08-06/07 NEN lobby outage.

    So: ask systemd which unit is active and restart that. Only fall back to trying both
    when neither is active (nothing is driving the screen anyway, so there is no session to
    duplicate).
    """
    kiosk_active = _run_systemctl(["is-active", "--quiet", "bluebird-kiosk.service"])[0]
    greetd_active = _run_systemctl(["is-active", "--quiet", "greetd"])[0]

    if kiosk_active and greetd_active:
        # Already the dual-compositor fault. Drop greetd and keep the real launcher rather
        # than restarting either — restarting would just re-race them.
        _run_systemctl(["stop", "greetd"])
        ok, msg = _run_systemctl(["restart", "bluebird-kiosk.service"])
        return ok, ("two compositors were running; stopped greetd and restarted "
                    "bluebird-kiosk.service: " + msg)
    if kiosk_active:
        ok, msg = _run_systemctl(["restart", "bluebird-kiosk.service"])
        return ok, "restarted bluebird-kiosk.service: " + msg
    if greetd_active:
        ok, msg = _run_systemctl(["restart", "greetd"])
        return ok, "restarted greetd: " + msg

    # Neither is up — nothing to duplicate, so try the configured-box launcher first.
    ok, msg = _run_systemctl(["restart", "bluebird-kiosk.service"])
    if ok:
        return True, "restarted bluebird-kiosk.service: " + msg
    ok2, msg2 = _run_systemctl(["restart", "greetd"])
    if ok2:
        return True, "restarted greetd: " + msg2
    return False, f"bluebird-kiosk.service: {msg} / greetd: {msg2}"


_COMMAND_EXECUTORS = {
    "take_screenshot": _cmd_take_screenshot,
    "force_sync": _cmd_force_sync,
    "check_update": _cmd_check_update,
    "restart_kiosk": _cmd_restart_kiosk,
    # Carries a payload: the operator's new password as a crypt hash. The
    # plaintext never reaches this device.
    "reset_access": _cmd_reset_access,
    # Carries a payload: {"output": "HDMI-A-1", "mode": "1920x1080@60Hz"}. Lets an
    # operator recover a screen stuck on an unusable EDID mode without going on site.
    "set_display_mode": _cmd_set_display_mode,
    # reboot is special-cased in _execute_commands: result is POSTed
    # BEFORE systemctl reboot, because afterwards there is no process
    # left to report it.
}

# Executors in this set are called with the command's parsed `args` dict;
# everything else is a bare verb called with no arguments. Keeping the split
# explicit means adding a payload to one command cannot change the calling
# convention of the others.
_COMMANDS_WITH_ARGS = frozenset({"reset_access", "set_display_mode"})


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
        # Commands that carry a payload declare it by accepting one argument.
        # Everything else stays a bare verb, so adding args here changed no
        # existing executor.
        raw_args = entry.get("args")
        try:
            if name in _COMMANDS_WITH_ARGS:
                parsed = {}
                if raw_args:
                    try:
                        parsed = json.loads(raw_args)
                    except (TypeError, ValueError):
                        parsed = {}
                ok, msg = executor(parsed)
            else:
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


_CONTENT_PATH = Path("/var/lib/bluebird-kiosk/display-content.json")


def _apply_output_assignments(oa: Any) -> None:
    """Translate per-output assignments into display-content.json + DISPLAY_LAYOUT=independent so
    the kiosk renders one chromium per output (launch-kiosk-independent). An empty/None map reverts
    to mirror (DISPLAY_LAYOUT=mirror). Restart only on a real change. A signage URL that can't be
    derived degrades that output to legacy_wall (fail-safe). Never breaks the heartbeat."""
    try:
        cfg = config.read_config()
        backend = cfg.get("BLUEBIRD_BACKEND") or ""
        cur_layout = cfg.get("DISPLAY_LAYOUT") or "mirror"
        if isinstance(oa, dict) and oa:
            content = {}
            for name, a in oa.items():
                if not isinstance(a, dict):
                    continue
                mode = str(a.get("mode") or "")
                if mode == "signage":
                    url = config.derive_beacon_url(
                        backend, str(a.get("slug") or ""), str(a.get("public_key") or ""))
                    entry = ({"mode": "signage", "url": url} if url
                             else {"mode": "legacy_wall", "url": ""})
                elif mode == "url":
                    # Admin cast (Screens pane "Cast a URL"): the cloud already validated the
                    # scheme, but re-check here — this string goes straight into Chromium.
                    raw = str(a.get("url") or "")
                    entry = ({"mode": "url", "url": raw}
                             if raw.startswith(("http://", "https://"))
                             else {"mode": "legacy_wall", "url": ""})
                else:
                    entry = {"mode": "legacy_wall", "url": ""}
                entry["touch"] = bool(a.get("touch"))   # which screen gets touch input
                content[str(name)] = entry
            try:
                cur = (json.loads(_CONTENT_PATH.read_text(encoding="utf-8")).get("outputs")
                       if _CONTENT_PATH.exists() else None)
            except (OSError, ValueError):
                cur = None
            if cur_layout == "independent" and cur == content:
                return  # no change — don't churn / restart
            # URL-only change (same outputs, same touch map, only mode/url differ — a cast
            # started / switched / expired / cleared): just rewrite the file. The reconcile
            # loop in launch-kiosk-independent notices the spec drift and cycles ONLY the
            # affected output's Chromium (≤15 s) — no full session restart, the other
            # screens never blink. A restart is still required when the output SET or the
            # touch map changes (session-start concerns: window placement, touch mapping).
            urls_only = (
                cur_layout == "independent" and isinstance(cur, dict)
                and set(cur.keys()) == set(content.keys())
                and all(bool((cur.get(k) or {}).get("touch")) == bool(content[k].get("touch"))
                        for k in content)
            )
            _CONTENT_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CONTENT_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"outputs": content}), encoding="utf-8")
            tmp.replace(_CONTENT_PATH)
            if urls_only:
                logger.info("heartbeat: per-output content url change (%d outputs) — "
                            "reconcile loop will converge without a restart", len(content))
                return
            config.write_config({"DISPLAY_LAYOUT": "independent"})
            logger.info("heartbeat: per-output content (%d outputs) — restarting kiosk", len(content))
            _cmd_restart_kiosk()
        elif cur_layout == "independent":
            # No per-output bindings anymore -> revert to mirror.
            try:
                _CONTENT_PATH.unlink()
            except OSError:
                pass
            config.write_config({"DISPLAY_LAYOUT": "mirror"})
            logger.info("heartbeat: per-output cleared -> mirror — restarting kiosk")
            _cmd_restart_kiosk()
    except Exception as exc:  # noqa: BLE001 — heartbeat must survive any failure here
        logger.warning("heartbeat: could not apply output assignments: %s", exc)


def _apply_display_assignment(assignment: Any) -> None:
    """Translate a Beacon/visitor `display_assignment` (bearer heartbeats only) into
    kiosk.conf so launch-kiosk-chromium picks the right URL on its next start:
      • {mode:'signage', slug, public_key} -> DISPLAY_MODE=signage + SIGNAGE_URL
      • {mode:'visitor', slug}             -> DISPLAY_MODE=visitor + VISITOR_URL (VMS 2b)
      • null / other                       -> DISPLAY_MODE=legacy_wall (revert)

    Writes all three keys every time so switching modes cleanly sets the active URL
    and clears the others. Only when the effective mode OR either url CHANGES do we
    rewrite the config and queue a kiosk restart so Chromium reloads onto the new
    target — we do NOT hot-swap a live URL, and we must NOT restart on every heartbeat.
    Best-effort: any failure is logged and never breaks the heartbeat or the emergency
    loopback."""
    try:
        cfg = config.read_config()
        backend = cfg.get("BLUEBIRD_BACKEND") or ""
        cur_mode = cfg.get("DISPLAY_MODE") or "legacy_wall"
        cur_signage = cfg.get("SIGNAGE_URL") or ""
        cur_visitor = cfg.get("VISITOR_URL") or ""
        mode = str(assignment.get("mode") or "") if isinstance(assignment, dict) else ""
        new_signage = ""
        new_visitor = ""
        if mode == "signage":
            new_signage = config.derive_beacon_url(
                backend, str(assignment.get("slug") or ""), str(assignment.get("public_key") or ""))
            new_mode = "signage" if new_signage else "legacy_wall"
        elif mode == "visitor":
            new_visitor = config.derive_visitor_url(backend, str(assignment.get("slug") or ""))
            new_mode = "visitor" if new_visitor else "legacy_wall"
        else:
            new_mode = "legacy_wall"
        if new_mode == cur_mode and new_signage == cur_signage and new_visitor == cur_visitor:
            return  # no change — never churn the config or restart
        config.write_config({
            "DISPLAY_MODE": new_mode,
            "SIGNAGE_URL": new_signage,
            "VISITOR_URL": new_visitor,
        })
        logger.info(
            "heartbeat: display mode %s -> %s (signage=%s visitor=%s) — restarting kiosk",
            cur_mode, new_mode, new_signage or "(none)", new_visitor or "(none)")
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
    output_health = _collect_output_health()
    if output_health:
        payload["output_health"] = output_health
    watchdog_state = _collect_watchdog_state()
    if watchdog_state:
        payload["watchdog"] = watchdog_state
    compositors = _collect_compositor_count()
    if compositors is not None:
        payload["compositor_count"] = compositors
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
    # Per-output content (one box, different content per screen). Takes precedence over the
    # single assignment via DISPLAY_LAYOUT=independent; absent/null reverts to mirror.
    if token and isinstance(data, dict) and "output_assignments" in data:
        _apply_output_assignments(data.get("output_assignments"))
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
