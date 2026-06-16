"""bluebird-watchdog — Phase 1 reliability daemon for BlueBird Kiosk OS.

Polls every 30 s for signs that the visible kiosk has wedged (sway dead,
chromium dead, local admin server unresponsive) and self-heals in
escalating steps before falling back to a full reboot.

State machine
-------------

    alive       — all checks pass; nothing to do
       │
       │ N consecutive failures
       ▼
    degraded   — at least one critical check has failed N times
       │       → action: systemctl restart greetd  (soft restart)
       │       → wait 30 s
       ▼
    recovering — post-restart probe pending
       │
       │ if still failing M more polls
       ▼
    rebooting  — last resort: systemctl reboot
       │       → cap: at most 1 reboot per REBOOT_COOLDOWN_SEC
       ▼
    alive (after reboot completes + checks pass again)

State is mirrored to /run/bluebird-kiosk/watchdog.json so the heartbeat
service can include it in its 60 s payload — the fleet console then
shows `degraded` / `recovering` badges in the grid without us needing
a second backend endpoint.

Tunable knobs (env or kiosk.conf-supplied via systemd EnvironmentFile):
  WATCHDOG_INTERVAL_SEC          — default 30
  WATCHDOG_FAIL_THRESHOLD        — default 3   (consecutive fails → degraded)
  WATCHDOG_RECOVERING_PROBES     — default 4   (post-restart probes before reboot)
  WATCHDOG_REBOOT_COOLDOWN_SEC   — default 1800 (don't reboot more than once
                                                  per 30 min)
  WATCHDOG_BOOT_GRACE_SEC        — default 120 (first ~2 min after boot, ignore
                                                 failures so sway has time to come up)
  WATCHDOG_DRY_RUN               — default 0   (1 = log decisions but don't act;
                                                 useful in dev)

Field-confirmed need (KI-010): the NEN kiosk repeatedly went offline
after a few hours of uptime — recovery required a manual power-cycle.
With this daemon, the same failure mode will self-recover within ~2
minutes of detection.
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
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("bluebird-kiosk.watchdog")

# ── Tunables (env overrides default) ─────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


INTERVAL_SEC          = _env_int("WATCHDOG_INTERVAL_SEC", 30)
FAIL_THRESHOLD        = _env_int("WATCHDOG_FAIL_THRESHOLD", 3)
RECOVERING_PROBES     = _env_int("WATCHDOG_RECOVERING_PROBES", 4)
REBOOT_COOLDOWN_SEC   = _env_int("WATCHDOG_REBOOT_COOLDOWN_SEC", 1800)
BOOT_GRACE_SEC        = _env_int("WATCHDOG_BOOT_GRACE_SEC", 120)
PAGE_STALE_SEC        = _env_int("WATCHDOG_PAGE_STALE_SEC", 120)
DRY_RUN               = bool(_env_int("WATCHDOG_DRY_RUN", 0))

STATE_PATH = Path("/run/bluebird-kiosk/watchdog.json")
LAST_REBOOT_PATH = Path("/var/lib/bluebird-kiosk/watchdog-last-reboot")
LOCAL_HEALTH_URL = "http://127.0.0.1:7311/_health"


# ── Health checks ────────────────────────────────────────────────────────────

def _read_uptime_sec() -> int:
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError):
        return 0


def _systemctl_is_active(unit: str) -> bool:
    """`systemctl is-active <unit>` returns 0 iff the unit reports active."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            timeout=5, check=False,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _process_running(pattern: str, *, exact: bool = False) -> bool:
    """`pgrep [-x|-f] <pattern>` exit 0 means at least one match.

    exact=True uses `-x` (exact process-NAME match), which is what you
    want for matching a top-level binary like `sway` regardless of its
    argv. exact=False uses `-f` (full cmdline regex), needed when the
    distinguishing detail is in argv (e.g. matching a specific chromium
    by --app=URL).
    """
    flag = "-x" if exact else "-f"
    try:
        r = subprocess.run(
            ["pgrep", flag, pattern],
            timeout=3, check=False, stdout=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _local_health() -> Tuple[bool, Optional[int]]:
    """Curl the local admin server's /_health (JSON) within 2 s and return
    (health_ok, page_idle_sec). page_idle_sec is the seconds since the displayed
    page last hit the local server; None if unknown/unparseable (treated as fresh
    so we never restart on a parse hiccup). curl (not `requests`) keeps the
    watchdog hot path dependency-free even if the venv is partially broken."""
    try:
        r = subprocess.run(
            ["curl", "-fsS", "--max-time", "2", LOCAL_HEALTH_URL],
            timeout=3, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, None
    if r.returncode != 0:
        return False, None
    try:
        data = json.loads((r.stdout or b"").decode("utf-8") or "{}")
        raw = data.get("page_idle_sec")
        idle = int(raw) if raw is not None else None
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        idle = None
    return True, idle


def _run_checks() -> Tuple[bool, Dict[str, bool]]:
    """Run every critical check, return (all_pass, per_check dict).

    Each check is a boolean that's True when the corresponding part of
    the stack is healthy. The watchdog escalates only when ALL of:
      greetd_active, sway_running, chromium_running, local_health
    are passing OR consistently failing — a single transient flake on
    one check doesn't trigger action.
    """
    health_ok, page_idle = _local_health()
    # page_fresh fails only when the local server is UP but the displayed page has
    # stopped driving it for PAGE_STALE_SEC — the signature of a wedged render
    # (e.g. a Cloudflare error page). Unknown idle → fresh (fail-safe). When the
    # local server itself is down, local_health already fails, so don't double-count.
    page_fresh = (page_idle is None) or (page_idle < PAGE_STALE_SEC)
    checks = {
        # exact=True so we match the top-level `sway` process by name,
        # not the swaybar/swaybg/swayidle children (cmdlines have `sway`
        # as a substring but they're not the compositor).
        "sway_running":     _process_running("sway", exact=True),
        # Full-cmdline match because we want THE kiosk chromium (with
        # --app=https://...), not the admin-overlay chromium or any
        # other chromium variant.
        "chromium_running": _process_running(r"chromium.*--app=https"),
        "local_health":     health_ok,
        # Wedged-page detector: see page_fresh computed above.
        "page_fresh":       page_fresh,
        # bluebird-admin runs the local server; if it's not active,
        # local_health will also be false, but we surface both so the
        # state file is diagnostic.
        "admin_active":     _systemctl_is_active("bluebird-admin"),
        # greetd is Type=idle on Ubuntu — it spawns the session and
        # exits. `systemctl is-active greetd` reports `inactive` even
        # when the session it spawned is alive. Surface for diagnostics
        # but DON'T gate health on it. sway_running is the real signal.
        "greetd_active":    _systemctl_is_active("greetd"),
    }
    # "Healthy" means every critical check passes. sway + chromium +
    # local_health are critical (no kiosk visible without all three).
    # admin_active is informational (its failure implies local_health=false).
    # greetd_active is informational only — see comment above.
    critical = ("sway_running", "chromium_running", "local_health", "page_fresh")
    all_pass = all(checks[k] for k in critical)
    return all_pass, checks


# ── State persistence ────────────────────────────────────────────────────────

def _write_state(state: str, fail_streak: int, last_checks: Dict[str, bool],
                 last_action: Optional[str] = None) -> None:
    """Atomic write of state.json under /run (tmpfs — no SSD wear).
    Heartbeat service reads this to include in its payload."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        payload = {
            "state": state,
            "fail_streak": fail_streak,
            "checks": last_checks,
            "last_action": last_action,
            "last_action_ts": int(time.time()),
            "hostname": socket.gethostname(),
        }
        tmp.write_text(json.dumps(payload))
        tmp.replace(STATE_PATH)
    except OSError as exc:
        logger.warning("watchdog: failed to write state file: %s", exc)


def _last_reboot_age_sec() -> int:
    """How long since we last triggered a reboot ourselves. Reads a
    persistent file at /var/lib (NOT /run — survives boots). Returns a
    huge number if no prior reboot record exists."""
    try:
        ts = int(LAST_REBOOT_PATH.read_text().strip())
        return max(0, int(time.time()) - ts)
    except (OSError, ValueError):
        return 10**9


def _record_reboot() -> None:
    """Persist 'we just triggered a reboot' before we actually call
    systemctl reboot. Path is under /var/lib so it survives the reboot
    and prevents reboot loops."""
    try:
        LAST_REBOOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_REBOOT_PATH.write_text(str(int(time.time())))
    except OSError as exc:
        logger.warning("watchdog: failed to record reboot timestamp: %s", exc)


# ── Recovery actions ─────────────────────────────────────────────────────────

def _restart_greetd() -> bool:
    """Soft heal: bounce greetd. That kills + respawns sway, which
    kills + respawns chromium. Handles the common 'something is wedged
    in the graphical session' case."""
    if DRY_RUN:
        logger.info("watchdog [DRY RUN]: would systemctl restart greetd")
        return True
    try:
        r = subprocess.run(
            ["systemctl", "restart", "greetd"],
            timeout=15, check=False,
        )
        logger.warning("watchdog: systemctl restart greetd → rc=%s", r.returncode)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("watchdog: greetd restart failed: %s", exc)
        return False


def _reboot_system() -> None:
    """Hard heal: full reboot. Last resort when restarting greetd
    didn't clear the wedge. Records the timestamp first so we honor
    REBOOT_COOLDOWN_SEC on the next boot."""
    _record_reboot()
    if DRY_RUN:
        logger.error("watchdog [DRY RUN]: would systemctl reboot (now)")
        return
    logger.error("watchdog: rebooting the system NOW (last-resort heal)")
    try:
        subprocess.run(["systemctl", "reboot"], timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.error("watchdog: reboot call failed: %s", exc)


# ── Main loop ────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info(
        "watchdog: starting (interval=%ss fail_threshold=%s recovering_probes=%s "
        "reboot_cooldown=%ss boot_grace=%ss dry_run=%s)",
        INTERVAL_SEC, FAIL_THRESHOLD, RECOVERING_PROBES,
        REBOOT_COOLDOWN_SEC, BOOT_GRACE_SEC, DRY_RUN,
    )

    state = "alive"
    fail_streak = 0
    recovering_probes_left = 0

    while True:
        all_pass, checks = _run_checks()
        uptime = _read_uptime_sec()

        # Boot grace window — sway + chromium + bluebird-admin can take
        # 30-60s to come up on a fresh boot. Don't escalate during that
        # window or we'd reboot a kiosk that's mid-startup.
        in_boot_grace = uptime < BOOT_GRACE_SEC

        if all_pass:
            if state != "alive":
                logger.info(
                    "watchdog: recovered → state=alive (was %s, checks=%s)",
                    state, checks,
                )
            state = "alive"
            fail_streak = 0
            recovering_probes_left = 0
            _write_state(state, fail_streak, checks)
        else:
            fail_streak += 1
            failed = [k for k, v in checks.items() if not v]
            logger.info(
                "watchdog: check fail streak=%s state=%s checks=%s failed=%s",
                fail_streak, state, checks, failed,
            )

            if in_boot_grace:
                logger.info(
                    "watchdog: in boot grace (%ss < %ss) — observing only",
                    uptime, BOOT_GRACE_SEC,
                )
                _write_state("boot_grace", fail_streak, checks)
            elif state == "alive" and fail_streak >= FAIL_THRESHOLD:
                # Escalate to degraded → soft heal
                state = "degraded"
                _write_state(state, fail_streak, checks,
                             last_action="restart_greetd")
                ok = _restart_greetd()
                state = "recovering"
                recovering_probes_left = RECOVERING_PROBES
                # Reset the streak — we'll start a fresh count after
                # restart. If checks STILL fail, we go to reboot.
                fail_streak = 0
                _write_state(state, fail_streak, checks,
                             last_action="restart_greetd" if ok else "restart_greetd_failed")
            elif state == "recovering":
                recovering_probes_left -= 1
                _write_state(state, fail_streak, checks)
                if recovering_probes_left <= 0:
                    # Soft heal didn't take. Reboot — but honor cooldown.
                    cooldown_remaining = REBOOT_COOLDOWN_SEC - _last_reboot_age_sec()
                    if cooldown_remaining > 0:
                        logger.error(
                            "watchdog: would reboot but cooldown blocks (%ss left)",
                            cooldown_remaining,
                        )
                        _write_state("cooldown", fail_streak, checks,
                                     last_action="reboot_blocked_cooldown")
                        # Reset so we'll re-try the soft heal first
                        # next time the streak hits threshold.
                        state = "alive"
                        recovering_probes_left = 0
                    else:
                        _write_state("rebooting", fail_streak, checks,
                                     last_action="reboot")
                        _reboot_system()
                        # If reboot doesn't take effect, we'll wake up and
                        # try again next tick. (Shouldn't happen.)
            else:
                # state == degraded or other transient state — keep counting
                _write_state(state, fail_streak, checks)

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
