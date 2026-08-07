"""Mode-selection hardening — _better_mode() in kiosk-display-manager.

EDID lies by omission. The NEN lobby LG advertises 4096x2160 (DCI-4K) five times and flags
NO preferred mode; the SMART IFP on the same box flags none either. With nothing preferred,
sway's choice is effectively arbitrary — and landing on DCI-4K means the kiosk drives an
output the panel cannot lock: "No Signal" on the glass while everything upstream reads green.
That cost two days on 2026-08-06/07.

The mode lists here are the real ones read off those two panels.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "apps").is_dir())
_DM = _ROOT / "build/live-build/config/includes.chroot/opt/bluebird-kiosk/bin/kiosk-display-manager"


def _load_dm():
    spec = importlib.util.spec_from_loader("kiosk_display_manager",
                                           importlib.machinery.SourceFileLoader(
                                               "kiosk_display_manager", str(_DM)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dm = _load_dm()


def _mode(w, h, hz):
    return {"width": w, "height": h, "refresh": hz * 1000}


def _out(name, cur_w, cur_h, cur_hz, modes):
    return {
        "name": name,
        "active": True,
        "current_mode": {"width": cur_w, "height": cur_h, "refresh": cur_hz * 1000},
        "modes": modes,
    }


# Real LG lobby panel: DCI-4K offered repeatedly, UHD available, nothing preferred.
_LG_MODES = [_mode(4096, 2160, 30), _mode(4096, 2160, 25), _mode(4096, 2160, 24),
             _mode(3840, 2160, 30), _mode(3840, 2160, 25), _mode(3840, 2160, 24),
             _mode(1920, 1080, 60), _mode(1920, 1080, 50), _mode(1280, 720, 60)]

# Real SMART IFP: 4K only at <=30Hz, 1080p at 60.
_SMART_MODES = [_mode(3840, 2160, 30), _mode(3840, 2160, 25), _mode(3840, 2160, 24),
                _mode(1920, 1080, 60), _mode(1920, 1080, 30), _mode(1280, 720, 60)]


def test_dci_4k_is_rejected_in_favour_of_uhd():
    """The exact fault: sway picked 4096x2160 and the panel showed nothing."""
    out = _out("HDMI-A-1", 4096, 2160, 30, _LG_MODES)
    assert dm._better_mode(out).startswith("3840x2160@")


def test_leaves_a_healthy_mode_alone():
    """Steady state must issue no commands — otherwise we fight the operator every event."""
    out = _out("HDMI-A-1", 1920, 1080, 60, _LG_MODES)
    assert dm._better_mode(out) == ""


def test_prefers_60hz_over_30hz_at_the_same_width():
    out = _out("DP-1", 1920, 1080, 30, _SMART_MODES)
    assert dm._better_mode(out) == "1920x1080@60Hz"


def test_leaves_4k30_alone_when_no_faster_4k_exists():
    """The SMART panel genuinely offers 4K only at <=30Hz. Downgrading resolution to chase
    refresh would be worse than leaving a sharp, working mode in place."""
    out = _out("DP-1", 3840, 2160, 30, _SMART_MODES)
    assert dm._better_mode(out) == ""


def test_no_modes_reported_is_a_no_op():
    out = {"name": "HDMI-A-1", "active": True,
           "current_mode": {"width": 4096, "height": 2160, "refresh": 30000}, "modes": []}
    assert dm._better_mode(out) == ""


def test_dci_4k_kept_only_if_no_uhd_offered():
    """Never strand an output with no picture: if DCI-4K is genuinely all there is, leave it."""
    modes = [_mode(4096, 2160, 30), _mode(4096, 2160, 24)]
    out = _out("HDMI-A-1", 4096, 2160, 30, modes)
    assert dm._better_mode(out) == ""


@pytest.mark.parametrize("bad", [
    {"name": "X", "active": True, "current_mode": {}, "modes": _LG_MODES},
    {"name": "X", "active": True, "modes": _LG_MODES},
])
def test_malformed_output_is_a_no_op(bad):
    assert dm._better_mode(bad) == ""
