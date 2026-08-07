"""Remote resolution control (set_display_mode).

Motivation: sway selects a mode from EDID, and a sink can advertise one it cannot lock. The
NEN lobby LG offers 4096x2160 (DCI-4K) five times and flags NO preferred mode, so some
restarts landed the kiosk on it — the panel showed "No Signal" while every fleet-console
signal read green, and recovery required physical access. That cost two days on 2026-08-06/07.

ARCHITECTURE NOTE, learned the hard way: bluebird-heartbeat.service runs as bluebird-kiosk
with ProtectSystem=strict + ProtectHome=yes and CANNOT reach sway's wayland socket. A first
implementation called display.list_outputs() directly from the executor and failed every
time in the field with "unknown output" — while working fine when run by hand outside the
sandbox. So the executor validates the output NAME against the published inventory, stages a
request file, and bluebird-set-display-mode.service (outside the sandbox) applies it and
re-checks the MODE against what wlr-randr actually advertises.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "apps").is_dir())
if str(_ROOT / "apps") not in sys.path:
    sys.path.insert(0, str(_ROOT / "apps"))

from bluebird_kiosk.services import heartbeat  # noqa: E402

_INVENTORY = [
    {"name": "DP-1", "mode": "1920x1080@60Hz", "active": True, "transform": "normal"},
    {"name": "HDMI-A-1", "mode": "1920x1080@60Hz", "active": True, "transform": "normal"},
]


@pytest.fixture()
def staged(monkeypatch, tmp_path):
    """Point the request file at tmp and stub the inventory + systemctl."""
    req = tmp_path / "display-mode.request"
    monkeypatch.setattr(heartbeat, "_DISPLAY_MODE_REQUEST", req)
    monkeypatch.setattr(heartbeat, "_collect_outputs", lambda: list(_INVENTORY))
    calls = []

    def fake_systemctl(args, timeout_s=None):
        calls.append(list(args))
        # The real unit consumes the request; emulate that so the executor's
        # "did it actually run?" check passes.
        if req.exists():
            req.unlink()
        return True, "ok"

    monkeypatch.setattr(heartbeat, "_run_systemctl", fake_systemctl)
    return {"req": req, "calls": calls}


def test_stages_a_request_and_starts_the_apply_unit(staged):
    ok, msg = heartbeat._cmd_set_display_mode({"output": "HDMI-A-1", "mode": "1920x1080@60Hz"})
    assert ok, msg
    assert ["start", "bluebird-set-display-mode.service"] in staged["calls"]
    assert not staged["req"].exists(), "the request must not be left on disk"


def test_request_payload_is_what_the_applier_expects(monkeypatch, tmp_path):
    req = tmp_path / "display-mode.request"
    monkeypatch.setattr(heartbeat, "_DISPLAY_MODE_REQUEST", req)
    monkeypatch.setattr(heartbeat, "_collect_outputs", lambda: list(_INVENTORY))
    seen = {}

    def fake_systemctl(args, timeout_s=None):
        seen["payload"] = json.loads(req.read_text(encoding="utf-8"))
        req.unlink()
        return True, "ok"

    monkeypatch.setattr(heartbeat, "_run_systemctl", fake_systemctl)
    heartbeat._cmd_set_display_mode({"output": "DP-1", "mode": "3840x2160@30Hz"})
    assert seen["payload"] == {"output": "DP-1", "mode": "3840x2160@30Hz"}


def test_refuses_an_output_not_in_the_inventory(staged):
    ok, msg = heartbeat._cmd_set_display_mode({"output": "HDMI-A-9", "mode": "1920x1080@60Hz"})
    assert not ok
    assert "unknown output" in msg
    assert staged["calls"] == [], "must not start the apply unit for an unknown output"


def test_missing_inventory_is_reported_not_guessed(monkeypatch, tmp_path):
    """Right after boot the display manager may not have published yet — say so plainly
    rather than rejecting a perfectly good output name."""
    monkeypatch.setattr(heartbeat, "_DISPLAY_MODE_REQUEST", tmp_path / "r.json")
    monkeypatch.setattr(heartbeat, "_collect_outputs", lambda: None)
    ok, msg = heartbeat._cmd_set_display_mode({"output": "HDMI-A-1", "mode": "1920x1080@60Hz"})
    assert not ok
    assert "inventory" in msg


@pytest.mark.parametrize("args", [
    {},
    {"output": "HDMI-A-1"},
    {"mode": "1920x1080@60Hz"},
    {"output": "HDMI-A-1", "mode": "; rm -rf /"},
    {"output": "HDMI-A-1; reboot", "mode": "1920x1080@60Hz"},
    {"output": "HDMI-A-1", "mode": "not-a-mode"},
])
def test_rejects_malformed_input_before_staging_anything(staged, args):
    ok, _ = heartbeat._cmd_set_display_mode(args)
    assert not ok
    assert staged["calls"] == []
    assert not staged["req"].exists()


def test_reports_when_the_apply_unit_never_consumed_the_request(monkeypatch, tmp_path):
    """A leftover request means the unit did not run — never claim success."""
    req = tmp_path / "display-mode.request"
    monkeypatch.setattr(heartbeat, "_DISPLAY_MODE_REQUEST", req)
    monkeypatch.setattr(heartbeat, "_collect_outputs", lambda: list(_INVENTORY))
    monkeypatch.setattr(heartbeat, "_run_systemctl", lambda a, timeout_s=None: (True, "ok"))
    ok, msg = heartbeat._cmd_set_display_mode({"output": "DP-1", "mode": "1920x1080@60Hz"})
    assert not ok
    assert "did not consume" in msg
    assert not req.exists(), "a stale request must never be left to replay later"


def test_failed_unit_start_clears_the_request(monkeypatch, tmp_path):
    req = tmp_path / "display-mode.request"
    monkeypatch.setattr(heartbeat, "_DISPLAY_MODE_REQUEST", req)
    monkeypatch.setattr(heartbeat, "_collect_outputs", lambda: list(_INVENTORY))
    monkeypatch.setattr(heartbeat, "_run_systemctl", lambda a, timeout_s=None: (False, "boom"))
    ok, msg = heartbeat._cmd_set_display_mode({"output": "DP-1", "mode": "1920x1080@60Hz"})
    assert not ok and "apply unit" in msg
    assert not req.exists()


def test_registered_as_an_args_carrying_command():
    assert "set_display_mode" in heartbeat._COMMAND_EXECUTORS
    assert "set_display_mode" in heartbeat._COMMANDS_WITH_ARGS


def test_applier_script_and_unit_ship():
    """The executor is useless without the unit that does the work."""
    unit = _ROOT / "build/live-build/config/includes.chroot/etc/systemd/system/bluebird-set-display-mode.service"
    script = _ROOT / "build/live-build/config/includes.chroot/opt/bluebird-kiosk/bin/set-display-mode"
    assert unit.is_file() and script.is_file()
    body = script.read_text(encoding="utf-8")
    # The mode check lives here because only this side can see the real mode list.
    assert "is not advertised by this display" in body
    # And it must persist, or the mode reverts on the next compositor restart.
    assert "display-settings.json" in body
