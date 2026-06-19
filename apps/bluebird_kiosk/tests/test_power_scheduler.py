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
                              side_effect=lambda on, reason: state.update({"on": on, "reason": reason})):
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
                              side_effect=lambda on, reason: state.update({"on": on, "reason": reason})):
        ps.main()
    assert state == {"on": True, "reason": "incident"}
