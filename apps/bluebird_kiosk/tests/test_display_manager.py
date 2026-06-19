"""Tests for the multi-display manager's decision logic — it must be idempotent (no command
churn / event-feedback loop) and a strict no-op on a single, already-correct display."""
import importlib.machinery
import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
_SCRIPT = _ROOT / ("build/live-build/config/includes.chroot"
                   "/opt/bluebird-kiosk/bin/kiosk-display-manager")


def _load():
    loader = importlib.machinery.SourceFileLoader("kiosk_display_manager", str(_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


dm = _load()


def test_mode_default_and_override(tmp_path, monkeypatch):
    f = tmp_path / "kiosk.conf"
    monkeypatch.setattr(dm, "CONF", f)
    assert dm._mode() == "mirror"                      # missing file -> default mirror
    f.write_text('KIOSK_DISPLAY_MODE="extend"\n', encoding="utf-8")
    assert dm._mode() == "extend"
    f.write_text("KIOSK_DISPLAY_MODE=mirror\n", encoding="utf-8")
    assert dm._mode() == "mirror"


def _capture(monkeypatch, outputs):
    cmds = []
    monkeypatch.setattr(dm, "_outputs", lambda: outputs)
    monkeypatch.setattr(dm, "_sway", lambda *a: cmds.append(a))
    return cmds


def test_single_display_is_a_noop(monkeypatch):
    # LIFE-SAFETY property: a lone, enabled, origin output must produce ZERO commands.
    cmds = _capture(monkeypatch, [{"name": "DP-1", "active": True, "rect": {"x": 0, "y": 0}}])
    dm.reconcile(mirror=True)
    assert cmds == []


def test_reconcile_noop_when_already_mirrored(monkeypatch):
    cmds = _capture(monkeypatch, [
        {"name": "DP-1", "active": True, "rect": {"x": 0, "y": 0}},
        {"name": "HDMI-A-1", "active": True, "rect": {"x": 0, "y": 0}},
    ])
    dm.reconcile(mirror=True)
    assert cmds == []                                  # steady state: no churn => no event loop


def test_reconcile_enables_and_stacks_new_output(monkeypatch):
    cmds = _capture(monkeypatch, [
        {"name": "DP-1", "active": True, "rect": {"x": 0, "y": 0}},           # already good
        {"name": "HDMI-A-1", "active": False, "rect": {"x": 1920, "y": 0}},   # just plugged in
    ])
    dm.reconcile(mirror=True)
    assert ("output", "HDMI-A-1", "enable") in cmds
    assert ("output", "HDMI-A-1", "position", "0", "0") in cmds
    assert not any(c[1] == "DP-1" for c in cmds)       # the healthy output is left alone


def test_reconcile_extend_enables_but_does_not_reposition(monkeypatch):
    cmds = _capture(monkeypatch, [
        {"name": "HDMI-A-1", "active": False, "rect": {"x": 1920, "y": 0}},
    ])
    dm.reconcile(mirror=False)
    assert ("output", "HDMI-A-1", "enable") in cmds
    assert not any("position" in c for c in cmds)      # extend mode: keep sway's layout
