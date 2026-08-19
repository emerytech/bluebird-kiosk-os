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


# ── Casts (mode:'url' — Screens pane "Cast a URL") ────────────────────────────

def test_apply_output_assignments_cast_url_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_CONTENT_PATH", tmp_path / "display-content.json")
    written, restarts = {}, []
    monkeypatch.setattr(hb.config, "read_config",
                        lambda: {"BLUEBIRD_BACKEND": "x", "DISPLAY_LAYOUT": "mirror"})
    monkeypatch.setattr(hb.config, "write_config", lambda v: written.update(v))
    monkeypatch.setattr(hb, "_cmd_restart_kiosk", lambda: (restarts.append(1), (True, "ok"))[1])
    hb._apply_output_assignments({
        "DP-1": {"mode": "url", "url": "https://docs.google.com/presentation/d/x/embed"},
    })
    content = json.loads(
        (tmp_path / "display-content.json").read_text(encoding="utf-8"))["outputs"]
    assert content["DP-1"] == {
        "mode": "url", "url": "https://docs.google.com/presentation/d/x/embed", "touch": False}
    # first flip mirror -> independent still needs the session restart
    assert written.get("DISPLAY_LAYOUT") == "independent" and restarts


def test_apply_output_assignments_cast_bad_scheme_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_CONTENT_PATH", tmp_path / "display-content.json")
    monkeypatch.setattr(hb.config, "read_config",
                        lambda: {"BLUEBIRD_BACKEND": "x", "DISPLAY_LAYOUT": "mirror"})
    monkeypatch.setattr(hb.config, "write_config", lambda v: None)
    monkeypatch.setattr(hb, "_cmd_restart_kiosk", lambda: (True, "ok"))
    hb._apply_output_assignments({
        "DP-1": {"mode": "url", "url": "javascript:alert(1)"},
        "DP-2": {"mode": "url", "url": ""},
    })
    content = json.loads(
        (tmp_path / "display-content.json").read_text(encoding="utf-8"))["outputs"]
    assert content["DP-1"]["mode"] == "legacy_wall"
    assert content["DP-2"]["mode"] == "legacy_wall"


def test_apply_output_assignments_url_only_change_skips_restart(tmp_path, monkeypatch):
    f = tmp_path / "display-content.json"
    f.write_text(json.dumps({"outputs": {
        "DP-1": {"mode": "legacy_wall", "url": "", "touch": False}}}), encoding="utf-8")
    monkeypatch.setattr(hb, "_CONTENT_PATH", f)
    written, restarts = {}, []
    monkeypatch.setattr(hb.config, "read_config",
                        lambda: {"BLUEBIRD_BACKEND": "x", "DISPLAY_LAYOUT": "independent"})
    monkeypatch.setattr(hb.config, "write_config", lambda v: written.update(v))
    monkeypatch.setattr(hb, "_cmd_restart_kiosk", lambda: (restarts.append(1), (True, "ok"))[1])
    hb._apply_output_assignments({
        "DP-1": {"mode": "url", "url": "https://example.com/deck"},
    })
    content = json.loads(f.read_text(encoding="utf-8"))["outputs"]
    assert content["DP-1"]["mode"] == "url"          # file rewritten…
    assert not restarts and not written               # …but no restart, no config churn


def test_apply_output_assignments_output_set_change_still_restarts(tmp_path, monkeypatch):
    f = tmp_path / "display-content.json"
    f.write_text(json.dumps({"outputs": {
        "DP-1": {"mode": "legacy_wall", "url": "", "touch": False}}}), encoding="utf-8")
    monkeypatch.setattr(hb, "_CONTENT_PATH", f)
    restarts = []
    monkeypatch.setattr(hb.config, "read_config",
                        lambda: {"BLUEBIRD_BACKEND": "x", "DISPLAY_LAYOUT": "independent"})
    monkeypatch.setattr(hb.config, "write_config", lambda v: None)
    monkeypatch.setattr(hb, "_cmd_restart_kiosk", lambda: (restarts.append(1), (True, "ok"))[1])
    hb._apply_output_assignments({
        "DP-1": {"mode": "url", "url": "https://example.com/deck"},
        "HDMI-A-1": {"mode": "legacy_wall"},          # new output appears -> placement restart
    })
    assert restarts


def test_apply_output_assignments_touch_change_still_restarts(tmp_path, monkeypatch):
    f = tmp_path / "display-content.json"
    f.write_text(json.dumps({"outputs": {
        "DP-1": {"mode": "legacy_wall", "url": "", "touch": False}}}), encoding="utf-8")
    monkeypatch.setattr(hb, "_CONTENT_PATH", f)
    restarts = []
    monkeypatch.setattr(hb.config, "read_config",
                        lambda: {"BLUEBIRD_BACKEND": "x", "DISPLAY_LAYOUT": "independent"})
    monkeypatch.setattr(hb.config, "write_config", lambda v: None)
    monkeypatch.setattr(hb, "_cmd_restart_kiosk", lambda: (restarts.append(1), (True, "ok"))[1])
    hb._apply_output_assignments({
        "DP-1": {"mode": "legacy_wall", "touch": True},   # touch map changed -> restart
    })
    assert restarts


# ── Tier 4: per-output render health (which screen is wedged) ──────────────────

def test_collect_output_health_reads_and_clamps(tmp_path, monkeypatch):
    f = tmp_path / "output-health.json"
    f.write_text(json.dumps({"ts": 1, "outputs": {
        "DP-1": {"wedged": True, "reloads": 3, "title": "503 Service Temporarily Unavailable"},
        "HDMI-A-1": {"wedged": False, "reloads": 0, "title": "Main Lobby — Beacon"},
    }}), encoding="utf-8")
    monkeypatch.setattr(hb, "_OUTPUT_HEALTH_PATH", f)
    h = hb._collect_output_health()
    assert h["DP-1"] == {"wedged": True, "reloads": 3,
                         "title": "503 Service Temporarily Unavailable"}
    assert h["HDMI-A-1"]["wedged"] is False


def test_collect_output_health_missing_or_empty_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_OUTPUT_HEALTH_PATH", tmp_path / "nope.json")
    assert hb._collect_output_health() is None
    f = tmp_path / "empty.json"; f.write_text('{"outputs": {}}', encoding="utf-8")
    monkeypatch.setattr(hb, "_OUTPUT_HEALTH_PATH", f)
    assert hb._collect_output_health() is None


def test_collect_output_health_bad_reloads_coerces(tmp_path, monkeypatch):
    f = tmp_path / "h.json"
    f.write_text(json.dumps({"outputs": {"DP-1": {"wedged": 1, "reloads": "x", "title": None}}}),
                 encoding="utf-8")
    monkeypatch.setattr(hb, "_OUTPUT_HEALTH_PATH", f)
    h = hb._collect_output_health()
    assert h["DP-1"]["reloads"] == 0 and h["DP-1"]["wedged"] is True and h["DP-1"]["title"] is None


# ── Tier 4: watchdog state forwarding ─────────────────────────────────────────

def test_collect_watchdog_state(tmp_path, monkeypatch):
    f = tmp_path / "watchdog.json"
    f.write_text(json.dumps({
        "state": "degraded", "fail_streak": 3,
        "checks": {"sway_running": True, "page_fresh": False},
        "last_action": "restart_greetd", "hostname": "k",
    }), encoding="utf-8")
    monkeypatch.setattr(hb, "_WATCHDOG_STATE_PATH", f)
    w = hb._collect_watchdog_state()
    assert w["state"] == "degraded" and w["fail_streak"] == 3
    assert w["checks"]["page_fresh"] is False and w["last_action"] == "restart_greetd"


def test_collect_watchdog_state_missing_or_stateless_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "_WATCHDOG_STATE_PATH", tmp_path / "nope.json")
    assert hb._collect_watchdog_state() is None
    f = tmp_path / "x.json"; f.write_text('{"fail_streak": 0}', encoding="utf-8")
    monkeypatch.setattr(hb, "_WATCHDOG_STATE_PATH", f)
    assert hb._collect_watchdog_state() is None   # no state -> None
