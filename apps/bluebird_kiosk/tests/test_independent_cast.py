"""Cast-a-URL resolution in launch-kiosk-independent.

An admin cast arrives in display-content.json as {mode:'url', url:...}. The reconcile loop
resolves each output's effective launch spec via _resolve_spec — a cast degrades to the Legacy
Wall when its URL is missing or an emergency incident is active (a foreign page can't render the
takeover modal). We source the script (the loop is guarded, sourcing just loads functions) and
call _resolve_spec against a temp display-content.json.
"""
import json
import subprocess
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = next(
    p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir()
)
_SCRIPT = _ROOT / (
    "build/live-build/config/includes.chroot"
    "/opt/bluebird-kiosk/bin/launch-kiosk-independent"
)


def _resolve(tmp_path, outputs: dict, name: str, incident: int) -> str:
    content = tmp_path / "display-content.json"
    content.write_text(json.dumps({"outputs": outputs}), encoding="utf-8")
    r = subprocess.run(
        ["bash", "-c",
         'source "$1"; CONTENT="$2"; _resolve_spec "$3" "$4"',
         "_", str(_SCRIPT), str(content), name, str(incident)],
        capture_output=True, text=True, timeout=20,
    )
    return r.stdout.strip()


def test_cast_url_resolves_to_url_mode(tmp_path):
    spec = _resolve(tmp_path, {"DP-1": {"mode": "url", "url": "https://example.com/deck"}},
                    "DP-1", incident=0)
    assert spec == "url|https://example.com/deck"


def test_cast_preempted_while_incident_active(tmp_path):
    spec = _resolve(tmp_path, {"DP-1": {"mode": "url", "url": "https://example.com/deck"}},
                    "DP-1", incident=1)
    assert spec == "legacy_wall|"


def test_cast_without_url_degrades_to_legacy_wall(tmp_path):
    spec = _resolve(tmp_path, {"DP-1": {"mode": "url", "url": ""}}, "DP-1", incident=0)
    assert spec == "legacy_wall|"


def test_signage_unaffected_by_incident_flag(tmp_path):
    # Signage is a first-party Beacon page — it carries the takeover modal itself, so the
    # incident override must NOT touch it (that path stays exactly as before casts existed).
    outputs = {"HDMI-A-1": {"mode": "signage", "url": "https://b/nen/beacon/d/KEY"}}
    assert _resolve(tmp_path, outputs, "HDMI-A-1", 0) == "signage|https://b/nen/beacon/d/KEY"
    assert _resolve(tmp_path, outputs, "HDMI-A-1", 1) == "signage|https://b/nen/beacon/d/KEY"


def test_unknown_output_falls_back_to_legacy_wall(tmp_path):
    assert _resolve(tmp_path, {}, "DP-9", 0) == "legacy_wall|"
