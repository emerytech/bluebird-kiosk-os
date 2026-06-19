"""Heartbeat reports the display output inventory (published by kiosk-display-manager) so the
fleet console / per-output binding can see each kiosk's screens."""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))   # apps/

from bluebird_kiosk.services import heartbeat as hb  # noqa: E402


def test_collect_outputs_reads_inventory(tmp_path, monkeypatch):
    f = tmp_path / "outputs.json"
    f.write_text(json.dumps([
        {"name": "DP-1", "mode": "1920x1080@60Hz", "active": True, "transform": "90"},
    ]), encoding="utf-8")
    monkeypatch.setattr(hb, "_OUTPUTS_PATH", f)
    assert hb._collect_outputs() == [
        {"name": "DP-1", "mode": "1920x1080@60Hz", "active": True, "transform": "90"}]


def test_collect_outputs_missing_or_empty_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_OUTPUTS_PATH", tmp_path / "nope.json")
    assert hb._collect_outputs() is None
    f = tmp_path / "empty.json"; f.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(hb, "_OUTPUTS_PATH", f)
    assert hb._collect_outputs() is None


def test_collect_outputs_caps_and_drops_nameless(tmp_path, monkeypatch):
    data = [{"name": "OUT-%d" % i} for i in range(20)] + [{"noname": 1}]
    f = tmp_path / "o.json"; f.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(hb, "_OUTPUTS_PATH", f)
    outs = hb._collect_outputs()
    assert len(outs) == 16 and all(o["name"] for o in outs)
