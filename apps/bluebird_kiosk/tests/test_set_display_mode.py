"""Remote resolution control (set_display_mode).

Motivation: sway selects a mode from EDID, and a sink can advertise a mode it cannot lock.
The NEN lobby LG offers 4096x2160 (DCI-4K) five times and flags NO preferred mode, so some
restarts landed the kiosk on it — the panel showed "No Signal" while every fleet-console
signal read green, and recovery required physical access. That cost two days on 2026-08-06/07.

The dangerous half of this feature is the write: setting an unsupported mode remotely blanks
a screen with nobody on site to undo it. Most of these tests pin the refusal paths.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "apps").is_dir())
if str(_ROOT / "apps") not in sys.path:
    sys.path.insert(0, str(_ROOT / "apps"))

from bluebird_kiosk.services import heartbeat  # noqa: E402


class _Out:
    def __init__(self, name, modes, current="1920x1080@60Hz", transform="normal"):
        self.name = name
        self.enabled = True
        self.current_mode = current
        self.available_modes = modes
        self.transform = transform


def _fake_display(monkeypatch, outputs, set_ok=True, set_msg="OK",
                  persist_ok=True, persist_msg="OK"):
    """Stub bluebird_kiosk.services.display; record calls for assertions."""
    calls = {"set_mode": [], "persist": 0}
    mod = types.ModuleType("bluebird_kiosk.services.display")

    def list_outputs():
        return outputs

    def set_mode(output, mode):
        calls["set_mode"].append((output, mode))
        return set_ok, set_msg

    def persist_settings():
        calls["persist"] += 1
        return persist_ok, persist_msg

    mod.list_outputs = list_outputs
    mod.set_mode = set_mode
    mod.persist_settings = persist_settings
    # Patch BOTH the module table and the package attribute. The executor does
    # `from . import display`, which resolves via the parent package's attribute once
    # anything else in the suite has imported the real module — so patching sys.modules
    # alone works in isolation and silently does nothing in a full run.
    import bluebird_kiosk.services as services_pkg
    monkeypatch.setitem(sys.modules, "bluebird_kiosk.services.display", mod)
    monkeypatch.setattr(services_pkg, "display", mod, raising=False)
    return calls


def test_sets_and_persists_an_advertised_mode(monkeypatch):
    outs = [_Out("HDMI-A-1", ["1920x1080@60Hz", "3840x2160@30Hz"])]
    calls = _fake_display(monkeypatch, outs)
    ok, msg = heartbeat._cmd_set_display_mode(
        {"output": "HDMI-A-1", "mode": "1920x1080@60Hz"})
    assert ok, msg
    assert calls["set_mode"] == [("HDMI-A-1", "1920x1080@60Hz")]
    # Durability matters as much as the change: without persist the mode reverts on the
    # next compositor restart and the drift that caused the outage returns.
    assert calls["persist"] == 1


def test_refuses_a_mode_the_output_does_not_advertise(monkeypatch):
    """The blackout guard. 4096x2160 is exactly what broke the NEN lobby panel."""
    outs = [_Out("HDMI-A-1", ["1920x1080@60Hz"])]
    calls = _fake_display(monkeypatch, outs)
    ok, msg = heartbeat._cmd_set_display_mode(
        {"output": "HDMI-A-1", "mode": "4096x2160@30Hz"})
    assert not ok
    assert "not advertised" in msg
    assert calls["set_mode"] == [], "must not touch the display when refusing"
    assert calls["persist"] == 0


def test_refuses_unknown_output(monkeypatch):
    outs = [_Out("DP-1", ["1920x1080@60Hz"])]
    calls = _fake_display(monkeypatch, outs)
    ok, msg = heartbeat._cmd_set_display_mode(
        {"output": "HDMI-A-1", "mode": "1920x1080@60Hz"})
    assert not ok
    assert "unknown output" in msg
    assert calls["set_mode"] == []


@pytest.mark.parametrize("args", [
    {},
    {"output": "HDMI-A-1"},
    {"mode": "1920x1080@60Hz"},
    {"output": "HDMI-A-1", "mode": "; rm -rf /"},
    {"output": "HDMI-A-1; reboot", "mode": "1920x1080@60Hz"},
    {"output": "HDMI-A-1", "mode": "not-a-mode"},
    {"output": "HDMI-A-1", "mode": "1920x1080@60Hz" + "0" * 64},
])
def test_rejects_malformed_input_before_touching_the_display(monkeypatch, args):
    calls = _fake_display(monkeypatch, [_Out("HDMI-A-1", ["1920x1080@60Hz"])])
    ok, _ = heartbeat._cmd_set_display_mode(args)
    assert not ok
    assert calls["set_mode"] == []


def test_reports_when_the_mode_applied_but_did_not_persist(monkeypatch):
    """A change that won't survive a restart must say so — silently succeeding here is
    how a 'fixed' kiosk quietly reverts overnight."""
    outs = [_Out("HDMI-A-1", ["1920x1080@60Hz"])]
    _fake_display(monkeypatch, outs, persist_ok=False, persist_msg="disk full")
    ok, msg = heartbeat._cmd_set_display_mode(
        {"output": "HDMI-A-1", "mode": "1920x1080@60Hz"})
    assert ok
    assert "NOT persisted" in msg and "revert" in msg


def test_surfaces_a_failed_set(monkeypatch):
    outs = [_Out("HDMI-A-1", ["1920x1080@60Hz"])]
    calls = _fake_display(monkeypatch, outs, set_ok=False, set_msg="wlr-randr exploded")
    ok, msg = heartbeat._cmd_set_display_mode(
        {"output": "HDMI-A-1", "mode": "1920x1080@60Hz"})
    assert not ok
    assert "wlr-randr exploded" in msg
    assert calls["persist"] == 0, "must not persist a mode that failed to apply"


def test_registered_as_an_args_carrying_command():
    assert "set_display_mode" in heartbeat._COMMAND_EXECUTORS
    assert "set_display_mode" in heartbeat._COMMANDS_WITH_ARGS
