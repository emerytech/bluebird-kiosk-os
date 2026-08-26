"""Beacon Inc 4 (kiosk side): signage URL derivation + the heartbeat's
display-assignment -> kiosk.conf translation.

The launcher's ?kiosk_cache=1 generalization (§13-C1) is bash and is guarded by
test_launcher_signage.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))  # apps/ (so `import bluebird_kiosk` resolves)

from bluebird_kiosk import config  # noqa: E402
from bluebird_kiosk.services import heartbeat  # noqa: E402


# ── derive_beacon_url ────────────────────────────────────────────────────────

def test_derive_beacon_url_builds_display_path():
    assert config.derive_beacon_url("https://bluebird-alerts.com", "nen", "abc123") == \
        "https://bluebird-alerts.com/nen/beacon/d/abc123"


def test_derive_beacon_url_strips_and_guards():
    assert config.derive_beacon_url("https://x.com/", "/nen/", "/k/") == "https://x.com/nen/beacon/d/k"
    assert config.derive_beacon_url("", "nen", "k") == ""
    assert config.derive_beacon_url("https://x.com", "nen", "") == ""
    assert config.derive_beacon_url("https://x.com", "", "k") == ""


# ── _apply_display_assignment ────────────────────────────────────────────────

def _run_apply(cfg, assignment):
    """Run _apply_display_assignment with config + restart mocked. Returns
    (writes, restart_count)."""
    writes: dict = {}
    restarts = []
    with mock.patch.object(heartbeat.config, "read_config", return_value=cfg), \
         mock.patch.object(heartbeat.config, "write_config", side_effect=writes.update), \
         mock.patch.object(heartbeat, "_cmd_restart_kiosk",
                           side_effect=lambda: (restarts.append(1), ("ok", ""))[1]):
        heartbeat._apply_display_assignment(assignment)
    return writes, len(restarts)


def test_signage_assignment_writes_conf_and_restarts():
    cfg = {"BLUEBIRD_BACKEND": "https://bluebird-alerts.com",
           "DISPLAY_MODE": "legacy_wall", "SIGNAGE_URL": ""}
    writes, restarts = _run_apply(cfg, {"mode": "signage", "slug": "nen", "public_key": "abc123"})
    assert writes["DISPLAY_MODE"] == "signage"
    assert writes["SIGNAGE_URL"] == "https://bluebird-alerts.com/nen/beacon/d/abc123"
    assert restarts == 1


def test_no_change_no_restart():
    # An identical assignment on a kiosk already in that mode must NOT churn the
    # config or restart (otherwise the kiosk would reboot every heartbeat).
    url = "https://bluebird-alerts.com/nen/beacon/d/abc123"
    cfg = {"BLUEBIRD_BACKEND": "https://bluebird-alerts.com",
           "DISPLAY_MODE": "signage", "SIGNAGE_URL": url}
    writes, restarts = _run_apply(cfg, {"mode": "signage", "slug": "nen", "public_key": "abc123"})
    assert writes == {}
    assert restarts == 0


def test_null_assignment_reverts_to_legacy_wall():
    cfg = {"BLUEBIRD_BACKEND": "https://bluebird-alerts.com",
           "DISPLAY_MODE": "signage", "SIGNAGE_URL": "https://x/nen/beacon/d/k"}
    writes, restarts = _run_apply(cfg, None)
    assert writes["DISPLAY_MODE"] == "legacy_wall"
    assert writes["SIGNAGE_URL"] == ""
    assert restarts == 1


def test_null_when_already_legacy_wall_is_noop():
    cfg = {"BLUEBIRD_BACKEND": "https://x", "DISPLAY_MODE": "legacy_wall", "SIGNAGE_URL": ""}
    writes, restarts = _run_apply(cfg, None)
    assert writes == {}
    assert restarts == 0


def test_unresolvable_signage_assignment_does_not_switch():
    # A signage assignment missing a public_key derives an empty URL -> stays in
    # legacy_wall mode (never points the kiosk at a broken URL).
    cfg = {"BLUEBIRD_BACKEND": "https://x", "DISPLAY_MODE": "legacy_wall", "SIGNAGE_URL": ""}
    writes, restarts = _run_apply(cfg, {"mode": "signage", "slug": "nen", "public_key": ""})
    assert writes == {}
    assert restarts == 0


# ── VMS Phase 2b — visitor mode (dashboard "designate as visitor kiosk") ──────

def test_visitor_assignment_writes_conf_and_restarts():
    cfg = {"BLUEBIRD_BACKEND": "https://bluebird-alerts.com",
           "DISPLAY_MODE": "legacy_wall", "SIGNAGE_URL": "", "VISITOR_URL": ""}
    writes, restarts = _run_apply(cfg, {"mode": "visitor", "slug": "nen"})
    assert writes["DISPLAY_MODE"] == "visitor"
    assert writes["VISITOR_URL"] == "https://bluebird-alerts.com/nen/visitor-kiosk"
    assert writes["SIGNAGE_URL"] == ""   # cleared
    assert restarts == 1


def test_switch_signage_to_visitor_clears_signage():
    cfg = {"BLUEBIRD_BACKEND": "https://bluebird-alerts.com",
           "DISPLAY_MODE": "signage", "SIGNAGE_URL": "https://bluebird-alerts.com/nen/beacon/d/k",
           "VISITOR_URL": ""}
    writes, restarts = _run_apply(cfg, {"mode": "visitor", "slug": "nen"})
    assert writes["DISPLAY_MODE"] == "visitor"
    assert writes["VISITOR_URL"] == "https://bluebird-alerts.com/nen/visitor-kiosk"
    assert writes["SIGNAGE_URL"] == ""
    assert restarts == 1


def test_visitor_no_change_no_restart():
    url = "https://bluebird-alerts.com/nen/visitor-kiosk"
    cfg = {"BLUEBIRD_BACKEND": "https://bluebird-alerts.com",
           "DISPLAY_MODE": "visitor", "SIGNAGE_URL": "", "VISITOR_URL": url}
    writes, restarts = _run_apply(cfg, {"mode": "visitor", "slug": "nen"})
    assert writes == {}
    assert restarts == 0


def test_null_reverts_visitor_to_legacy_wall():
    cfg = {"BLUEBIRD_BACKEND": "https://bluebird-alerts.com",
           "DISPLAY_MODE": "visitor", "SIGNAGE_URL": "",
           "VISITOR_URL": "https://bluebird-alerts.com/nen/visitor-kiosk"}
    writes, restarts = _run_apply(cfg, None)
    assert writes["DISPLAY_MODE"] == "legacy_wall"
    assert writes["VISITOR_URL"] == ""
    assert restarts == 1


def test_unresolvable_visitor_assignment_does_not_switch():
    # A visitor assignment with no slug derives an empty URL -> stays legacy_wall.
    cfg = {"BLUEBIRD_BACKEND": "https://x", "DISPLAY_MODE": "legacy_wall",
           "SIGNAGE_URL": "", "VISITOR_URL": ""}
    writes, restarts = _run_apply(cfg, {"mode": "visitor", "slug": ""})
    assert writes == {}
    assert restarts == 0
