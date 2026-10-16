"""Tests for the display power scheduler's decision logic.

The scheduler ships as a standalone root script (not a package module), so we
load it by path. The focus is _desired_on()'s FAIL-SAFE contract: any missing,
disabled, or malformed schedule must keep the display ON. A safety-critical
wall must never go dark by accident.
"""
import importlib.machinery
import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

_HERE = Path(__file__).resolve()
_ROOT = next(
    p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir()
)
_SCRIPT = _ROOT / (
    "build/live-build/config/includes.chroot"
    "/opt/bluebird-kiosk/bin/kiosk-power-scheduler"
)


def _load():
    # The script has no .py extension, so spec_from_file_location can't infer a
    # loader — build one explicitly from the source file.
    loader = importlib.machinery.SourceFileLoader("kiosk_power_scheduler", str(_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ps = _load()
_REAL_BOOT_ID = ps._boot_id          # the unpatched function, for its own test below


@pytest.fixture(autouse=True)
def _pinned_boot_id():
    # The applied record is keyed on the boot id (see _boot_id). Pin it so the tests
    # are deterministic on hosts without /proc (macOS) and a reboot can be simulated.
    with mock.patch.object(ps, "_boot_id", return_value="boot-A"):
        yield


def _at(dt, schedule):
    """Evaluate _desired_on with the scheduler's clock pinned to `dt` (naive,
    so the no-timezone branch is exercised deterministically)."""
    with mock.patch.object(ps, "datetime") as m:
        m.now.return_value = dt
        return ps._desired_on(schedule)


# ── fail-safe: anything ambiguous => ON ───────────────────────────────────────

def test_disabled_is_on():
    assert ps._desired_on(
        {"enabled": False, "on": "07:00", "off": "18:00", "days": [0]}
    ) is True


def test_empty_is_on():
    assert ps._desired_on({}) is True
    assert ps._desired_on(None) is True


def test_missing_days_is_on():
    assert ps._desired_on({"enabled": True, "on": "07:00", "off": "18:00"}) is True


def test_inverted_window_is_on():
    dt = datetime(2026, 6, 15, 23, 0)
    sched = {"enabled": True, "on": "18:00", "off": "07:00", "days": [dt.weekday()]}
    assert _at(dt, sched) is True


def test_bad_time_is_on():
    dt = datetime(2026, 6, 15, 23, 0)
    sched = {"enabled": True, "on": "oops", "off": "18:00", "days": [dt.weekday()]}
    assert _at(dt, sched) is True


# ── normal on/off behavior ────────────────────────────────────────────────────

def test_within_window_on_active_day_is_on():
    dt = datetime(2026, 6, 15, 9, 0)
    sched = {"enabled": True, "on": "07:00", "off": "18:00", "days": [dt.weekday()]}
    assert _at(dt, sched) is True


def test_before_window_is_off():
    dt = datetime(2026, 6, 15, 6, 0)
    sched = {"enabled": True, "on": "07:00", "off": "18:00", "days": [dt.weekday()]}
    assert _at(dt, sched) is False


def test_after_window_is_off():
    dt = datetime(2026, 6, 15, 20, 0)
    sched = {"enabled": True, "on": "07:00", "off": "18:00", "days": [dt.weekday()]}
    assert _at(dt, sched) is False


def test_non_scheduled_day_is_off():
    dt = datetime(2026, 6, 15, 9, 0)
    other_day = (dt.weekday() + 1) % 7
    sched = {"enabled": True, "on": "07:00", "off": "18:00", "days": [other_day]}
    assert _at(dt, sched) is False


# ── _parse_hhmm ───────────────────────────────────────────────────────────────

def test_parse_hhmm():
    assert ps._parse_hhmm("07:30") == 450
    assert ps._parse_hhmm("00:00") == 0
    assert ps._parse_hhmm("23:59") == 23 * 60 + 59
    assert ps._parse_hhmm("24:00") is None
    assert ps._parse_hhmm("bad") is None
    assert ps._parse_hhmm(None) is None


# ── touch-to-wake override (only ever forces ON; safe direction) ───────────────

def _write_wake(p, wake_until):
    p.write_text(json.dumps({"wake_until": wake_until}), encoding="utf-8")


def test_wake_active_future_true(tmp_path):
    f = tmp_path / "wake.json"; _write_wake(f, time.time() + 300)
    with mock.patch.object(ps, "WAKE_PATH", f):
        assert ps._wake_active() is True


def test_wake_active_expired_false(tmp_path):
    f = tmp_path / "wake.json"; _write_wake(f, time.time() - 1)
    with mock.patch.object(ps, "WAKE_PATH", f):
        assert ps._wake_active() is False


def test_wake_active_missing_or_malformed_false(tmp_path):
    f = tmp_path / "wake.json"
    with mock.patch.object(ps, "WAKE_PATH", f):
        assert ps._wake_active() is False          # missing file
    f.write_text("not json", encoding="utf-8")
    with mock.patch.object(ps, "WAKE_PATH", f):
        assert ps._wake_active() is False          # malformed


def test_main_wake_override_forces_on_during_off_hours(tmp_path):
    f = tmp_path / "wake.json"; _write_wake(f, time.time() + 300)
    applied, state = {}, {}
    with mock.patch.object(ps, "WAKE_PATH", f), \
            mock.patch.object(ps, "_emergency_active", return_value=False), \
            mock.patch.object(ps, "_read_schedule", return_value={"enabled": True}), \
            mock.patch.object(ps, "_desired_on", return_value=False), \
            mock.patch.object(ps, "_apply", side_effect=lambda on: applied.__setitem__("on", on)), \
            mock.patch.object(ps, "_write_state",
                              side_effect=lambda on, reason, applied=None: state.update({"on": on, "reason": reason})):
        rc = ps.main()
    assert rc == 0 and applied["on"] is True
    assert state == {"on": True, "reason": "wake_override"}


def test_main_expired_wake_lets_schedule_blank(tmp_path):
    f = tmp_path / "wake.json"; _write_wake(f, time.time() - 1)
    applied = {}
    with mock.patch.object(ps, "WAKE_PATH", f), \
            mock.patch.object(ps, "_emergency_active", return_value=False), \
            mock.patch.object(ps, "_read_schedule", return_value={"enabled": True}), \
            mock.patch.object(ps, "_desired_on", return_value=False), \
            mock.patch.object(ps, "_apply", side_effect=lambda on: applied.__setitem__("on", on)), \
            mock.patch.object(ps, "_write_state"):
        ps.main()
    assert applied["on"] is False                  # schedule wins; not wrongly kept awake


def test_main_emergency_beats_everything(tmp_path):
    # an active incident forces ON even with no wake override — emergency precedence preserved
    f = tmp_path / "wake.json"; _write_wake(f, time.time() - 1)
    state = {}
    with mock.patch.object(ps, "WAKE_PATH", f), \
            mock.patch.object(ps, "_emergency_active", return_value=True), \
            mock.patch.object(ps, "_apply", return_value=None), \
            mock.patch.object(ps, "_write_state",
                              side_effect=lambda on, reason, applied=None: state.update({"on": on, "reason": reason})):
        ps.main()
    assert state == {"on": True, "reason": "incident"}


# ── _sway_socket: pick the LIVE socket, not the lexically-first (dead) one ─────
#
# Regression: sway restarts once at boot (greetd conflict dance), so two sockets
# exist — sway-ipc.<uid>.<oldpid>.sock (dead) and ...<newpid>.sock (live). The old
# `sorted(globs)[0]` picked the oldest/dead one and every dpms toggle then failed.

def test_socket_pid_parsing():
    assert ps._socket_pid("/run/user/999/sway-ipc.999.2507.sock") == 2507
    assert ps._socket_pid("/run/user/999/sway-ipc.999.1465.sock") == 1465
    assert ps._socket_pid("/run/user/999/garbage.sock") is None
    assert ps._socket_pid("no-sock-suffix") is None


def test_sway_socket_prefers_live_pid_over_lexically_first():
    both = ["/run/user/999/sway-ipc.999.1465.sock",   # lexically first, DEAD
            "/run/user/999/sway-ipc.999.2507.sock"]   # live
    with mock.patch.object(ps.glob, "glob", return_value=both), \
            mock.patch.object(ps, "_pid_is_sway", side_effect=lambda pid: pid == 2507):
        assert ps._sway_socket(999) == "/run/user/999/sway-ipc.999.2507.sock"


def test_sway_socket_none_when_empty():
    with mock.patch.object(ps.glob, "glob", return_value=[]):
        assert ps._sway_socket(999) is None


def test_sway_socket_falls_back_to_newest_mtime_when_none_live(tmp_path):
    older = tmp_path / "sway-ipc.999.1.sock"; older.write_text("")
    newer = tmp_path / "sway-ipc.999.2.sock"; newer.write_text("")
    import os
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    with mock.patch.object(ps.glob, "glob", return_value=[str(older), str(newer)]), \
            mock.patch.object(ps, "_pid_is_sway", return_value=False):
        assert ps._sway_socket(999) == str(newer)   # newest mtime wins as a safe fallback


# ── _apply: send dpms only when the desired state differs from the last applied ───
#
# Field 2026-10-16 (NEN lobby touch board): `swaymsg output * dpms on` was re-sent
# EVERY minute while the panel was already on. Each one is a round trip through
# sway that showed as a periodic hitch in the touch UI. The scheduler now records
# what it actually applied ({dpms, sock, at} under "applied" in power_state.json)
# and skips a redundant call — but only when the previous run positively applied
# the SAME verb through the SAME live sway socket within REASSERT_SECS. Anything
# else re-sends: that is the fail-safe direction for a display that must be ON.

SOCK = "/run/user/999/sway-ipc.999.2507.sock"


def _sway_up(sock=SOCK):
    fake = mock.Mock()
    fake.pw_uid = 999
    return (mock.patch.object(ps.pwd, "getpwnam", return_value=fake),
            mock.patch.object(ps, "_sway_socket", return_value=sock))


def _ok():
    return mock.Mock(returncode=0, stdout="", stderr="")


def _dpms_verbs(run_mock):
    return [c.args[0][-1] for c in run_mock.call_args_list]


def test_apply_sends_when_nothing_was_applied_before():
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "_last_applied", return_value={}), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        rec = ps._apply(True)
    assert _dpms_verbs(run) == ["on"]
    assert rec["dpms"] == "on" and rec["sock"] == SOCK
    assert abs(rec["at"] - time.time()) < 5
    assert rec["boot"] == "boot-A"


def test_apply_skips_redundant_call_and_keeps_previous_record():
    prev = {"dpms": "on", "sock": SOCK, "at": int(time.time()) - 60, "boot": "boot-A"}
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "_last_applied", return_value=prev), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        rec = ps._apply(True)
    assert run.call_count == 0
    assert rec == prev


def test_apply_resends_when_the_verb_changes():
    prev = {"dpms": "on", "sock": SOCK, "at": int(time.time()) - 60}
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "_last_applied", return_value=prev), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        rec = ps._apply(False)
    assert _dpms_verbs(run) == ["off"] and rec["dpms"] == "off"


def test_apply_resends_after_sway_restart_new_socket():
    prev = {"dpms": "on", "sock": "/run/user/999/sway-ipc.999.1465.sock", "at": int(time.time()) - 60}
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "_last_applied", return_value=prev), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        ps._apply(True)
    assert _dpms_verbs(run) == ["on"]


def test_apply_reasserts_after_the_slow_window():
    prev = {"dpms": "on", "sock": SOCK, "at": int(time.time()) - ps.REASSERT_SECS - 1}
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "_last_applied", return_value=prev), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        ps._apply(True)
    assert _dpms_verbs(run) == ["on"]


def test_apply_resends_after_a_reboot_even_if_the_socket_path_repeats():
    # sway's socket path embeds its pid, and a deterministic boot can hand out the same
    # pid again. Across a reboot the panel is back in its default-ON state, so a persisted
    # "off applied" record must not suppress the first scheduled-off call of the new boot.
    prev = {"dpms": "off", "sock": SOCK, "at": int(time.time()) - 60, "boot": "boot-A"}
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "_boot_id", return_value="boot-B"), \
            mock.patch.object(ps, "_last_applied", return_value=prev), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        rec = ps._apply(False)
    assert _dpms_verbs(run) == ["off"] and rec["boot"] == "boot-B"


def test_apply_resends_when_the_boot_id_is_missing_from_the_record_or_unreadable():
    pw, sw = _sway_up()
    prev = {"dpms": "on", "sock": SOCK, "at": int(time.time()) - 60}       # pre-boot-id record
    with pw, sw, mock.patch.object(ps, "_last_applied", return_value=prev), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        ps._apply(True)
    assert _dpms_verbs(run) == ["on"]
    prev["boot"] = ""
    with pw, sw, mock.patch.object(ps, "_boot_id", return_value=""), \
            mock.patch.object(ps, "_last_applied", return_value=prev), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        ps._apply(True)
    assert _dpms_verbs(run) == ["on"]                                        # unreadable => never trust


def test_boot_id_reads_proc_and_is_empty_when_unreadable(tmp_path):
    f = tmp_path / "boot_id"
    f.write_text("0b6d1b7a-5f1e-4c53-9d7a-2b1c3d4e5f60\n", encoding="utf-8")
    with mock.patch.object(ps, "BOOT_ID_PATH", f):
        assert _REAL_BOOT_ID() == "0b6d1b7a-5f1e-4c53-9d7a-2b1c3d4e5f60"
    with mock.patch.object(ps, "BOOT_ID_PATH", tmp_path / "missing"):
        assert _REAL_BOOT_ID() == ""


def test_apply_resends_on_garbage_or_future_timestamp():
    pw, sw = _sway_up()
    for at in ("bogus", None, time.time() + 3600):
        prev = {"dpms": "on", "sock": SOCK, "at": at}
        with pw, sw, mock.patch.object(ps, "_last_applied", return_value=prev), \
                mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
            ps._apply(True)
        assert _dpms_verbs(run) == ["on"], at


def test_apply_failed_call_returns_empty_so_the_next_run_retries():
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "_last_applied", return_value={}), \
            mock.patch.object(ps.subprocess, "run",
                              return_value=mock.Mock(returncode=1, stdout="", stderr="Unable to connect")):
        assert ps._apply(True) == {}
    with pw, sw, mock.patch.object(ps, "_last_applied", return_value={}), \
            mock.patch.object(ps.subprocess, "run", side_effect=OSError("no sudo")):
        assert ps._apply(True) == {}


def test_apply_without_a_session_returns_empty_and_sends_nothing():
    fake = mock.Mock()
    fake.pw_uid = 999
    with mock.patch.object(ps.pwd, "getpwnam", return_value=fake), \
            mock.patch.object(ps, "_sway_socket", return_value=None), \
            mock.patch.object(ps.subprocess, "run") as run:
        assert ps._apply(True) == {}
    assert run.call_count == 0


# ── state file: applied record round-trips; existing fields unchanged ─────────

def test_write_state_persists_applied_and_keeps_scheduled_off_contract(tmp_path):
    f = tmp_path / "power_state.json"
    rec = {"dpms": "on", "sock": SOCK, "at": 1234}
    with mock.patch.object(ps, "STATE_PATH", f):
        ps._write_state(True, "schedule", rec)
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["display_on"] is True and data["scheduled_off"] is False
        assert data["applied"] == rec
        assert ps._last_applied() == rec
        ps._write_state(False, "schedule", {})
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["scheduled_off"] is True and data["applied"] == {}
        ps._write_state(True, "incident")                     # old 2-arg call still works
        assert json.loads(f.read_text(encoding="utf-8"))["applied"] == {}


def test_read_state_missing_or_malformed_is_empty(tmp_path):
    f = tmp_path / "power_state.json"
    with mock.patch.object(ps, "STATE_PATH", f):
        assert ps._read_state() == {} and ps._last_applied() == {}
        f.write_text("garbage", encoding="utf-8")
        assert ps._read_state() == {} and ps._last_applied() == {}
        f.write_text(json.dumps({"applied": "not-a-dict"}), encoding="utf-8")
        assert ps._last_applied() == {}


def test_main_threads_the_applied_record_into_state():
    rec = {"dpms": "on", "sock": SOCK, "at": 1}
    seen = {}
    with mock.patch.object(ps, "_emergency_active", return_value=False), \
            mock.patch.object(ps, "_read_schedule", return_value={}), \
            mock.patch.object(ps, "_apply", return_value=rec), \
            mock.patch.object(ps, "_write_state",
                              side_effect=lambda on, reason, applied=None: seen.update(
                                  {"on": on, "reason": reason, "applied": applied})):
        ps.main()
    assert seen == {"on": True, "reason": "no_schedule", "applied": rec}


# ── minute-by-minute: one swaymsg per transition, quiet in between ────────────

def test_minute_loop_sends_once_then_stays_quiet(tmp_path):
    f = tmp_path / "power_state.json"
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "STATE_PATH", f), \
            mock.patch.object(ps, "_emergency_active", return_value=False), \
            mock.patch.object(ps, "_read_schedule", return_value={}), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        for _ in range(5):
            assert ps.main() == 0
    assert _dpms_verbs(run) == ["on"]                 # was 5x "on" before the change


def test_minute_loop_resends_on_each_transition_only(tmp_path):
    f = tmp_path / "power_state.json"
    wake = tmp_path / "wake.json"
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "STATE_PATH", f), mock.patch.object(ps, "WAKE_PATH", wake), \
            mock.patch.object(ps, "_emergency_active", return_value=False), \
            mock.patch.object(ps, "_read_schedule", return_value={"enabled": True}), \
            mock.patch.object(ps, "_desired_on", return_value=False), \
            mock.patch.object(ps.subprocess, "run", return_value=_ok()) as run:
        ps.main(); ps.main()                                     # off-hours: blank once
        _write_wake(wake, time.time() + 300)                     # tap: wake override
        ps.main(); ps.main()                                     # on once, then quiet
        _write_wake(wake, time.time() - 1)                       # override expired
        ps.main(); ps.main()                                     # back off once, then quiet
    assert _dpms_verbs(run) == ["off", "on", "off"]


def test_minute_loop_retries_after_a_failed_call(tmp_path):
    f = tmp_path / "power_state.json"
    pw, sw = _sway_up()
    with pw, sw, mock.patch.object(ps, "STATE_PATH", f), \
            mock.patch.object(ps, "_emergency_active", return_value=False), \
            mock.patch.object(ps, "_read_schedule", return_value={}), \
            mock.patch.object(ps.subprocess, "run",
                              side_effect=[mock.Mock(returncode=1, stdout="", stderr="x"), _ok(), _ok()]) as run:
        ps.main(); ps.main(); ps.main()
    assert run.call_count == 2                        # failed, retried OK, then quiet
