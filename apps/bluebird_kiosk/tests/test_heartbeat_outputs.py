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


# ── Phase C: applying output_assignments -> display-content.json + DISPLAY_LAYOUT ──

def test_apply_output_assignments_writes_content_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_CONTENT_PATH", tmp_path / "display-content.json")
    cfg = {"BLUEBIRD_BACKEND": "https://bluebird-alerts.com", "DISPLAY_LAYOUT": "mirror"}
    written, restarts = {}, []
    monkeypatch.setattr(hb.config, "read_config", lambda: cfg)
    monkeypatch.setattr(hb.config, "write_config", lambda v: written.update(v))
    monkeypatch.setattr(hb, "_cmd_restart_kiosk", lambda: (restarts.append(1), (True, "ok"))[1])
    hb._apply_output_assignments({
        "HDMI-A-1": {"mode": "signage", "slug": "nen", "public_key": "KEY"},
        "DP-1": {"mode": "legacy_wall"},
    })
    content = json.loads((tmp_path / "display-content.json").read_text(encoding="utf-8"))["outputs"]
    assert content["HDMI-A-1"]["mode"] == "signage"
    assert content["HDMI-A-1"]["url"].endswith("/nen/beacon/d/KEY")
    assert content["DP-1"]["mode"] == "legacy_wall" and content["DP-1"]["touch"] is False
    assert written.get("DISPLAY_LAYOUT") == "independent" and restarts


def test_apply_output_assignments_reverts_to_mirror(tmp_path, monkeypatch):
    f = tmp_path / "display-content.json"; f.write_text('{"outputs": {}}', encoding="utf-8")
    monkeypatch.setattr(hb, "_CONTENT_PATH", f)
    written, restarts = {}, []
    monkeypatch.setattr(hb.config, "read_config", lambda: {"DISPLAY_LAYOUT": "independent"})
    monkeypatch.setattr(hb.config, "write_config", lambda v: written.update(v))
    monkeypatch.setattr(hb, "_cmd_restart_kiosk", lambda: (restarts.append(1), (True, "ok"))[1])
    hb._apply_output_assignments(None)
    assert written.get("DISPLAY_LAYOUT") == "mirror" and not f.exists() and restarts


def test_apply_output_assignments_noop_when_unchanged(tmp_path, monkeypatch):
    f = tmp_path / "display-content.json"
    f.write_text(json.dumps({"outputs": {"DP-1": {"mode": "legacy_wall", "url": "", "touch": False}}}), encoding="utf-8")
    monkeypatch.setattr(hb, "_CONTENT_PATH", f)
    restarts = []
    monkeypatch.setattr(hb.config, "read_config", lambda: {"BLUEBIRD_BACKEND": "x", "DISPLAY_LAYOUT": "independent"})
    monkeypatch.setattr(hb.config, "write_config", lambda v: None)
    monkeypatch.setattr(hb, "_cmd_restart_kiosk", lambda: (restarts.append(1), (True, "ok"))[1])
    hb._apply_output_assignments({"DP-1": {"mode": "legacy_wall"}})
    assert not restarts   # identical -> no churn / no restart
