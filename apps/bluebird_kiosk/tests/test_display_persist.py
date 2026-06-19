"""display.persist_settings snapshots the current display config so kiosk-display-manager can
restore it after a reboot / unattended update."""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))   # apps/ -> makes `bluebird_kiosk` importable

from bluebird_kiosk.services import display  # noqa: E402


def test_persist_settings_snapshots_outputs_and_brightness(tmp_path, monkeypatch):
    monkeypatch.setattr(display, "SETTINGS_PATH", tmp_path / "ds.json")
    monkeypatch.setattr(display, "get_brightness", lambda: 70)
    monkeypatch.setattr(display, "list_outputs", lambda: [
        display.DisplayOutput(name="DP-1", enabled=True, current_mode="1920x1080@60Hz",
                              available_modes=[], transform="90"),
        display.DisplayOutput(name="HDMI-A-1", enabled=False, current_mode="",
                              available_modes=[], transform="normal"),  # disabled -> excluded
    ])
    ok, msg = display.persist_settings()
    assert ok, msg
    data = json.loads((tmp_path / "ds.json").read_text(encoding="utf-8"))
    assert data["brightness"] == 70
    assert data["outputs"] == {"DP-1": {"transform": "90", "mode": "1920x1080@60Hz"}}


def test_persist_settings_handles_no_backlight(tmp_path, monkeypatch):
    monkeypatch.setattr(display, "SETTINGS_PATH", tmp_path / "ds.json")
    monkeypatch.setattr(display, "get_brightness", lambda: None)   # desktop box, no backlight
    monkeypatch.setattr(display, "list_outputs", lambda: [])
    ok, _ = display.persist_settings()
    assert ok
    data = json.loads((tmp_path / "ds.json").read_text(encoding="utf-8"))
    assert data["brightness"] is None and data["outputs"] == {}
