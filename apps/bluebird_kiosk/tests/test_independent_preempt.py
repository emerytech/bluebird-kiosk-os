"""Fast emergency preemption of cast outputs in launch-kiosk-independent.

When an incident is active, _preempt_casts_on_incident must kill the foreign Chromium on EVERY
cast (mode:'url') output up front, and leave non-cast outputs (legacy_wall / signage) alone. We
source the script, stub `_incident_active`, and shim `pgrep`/`pkill` on PATH so we can observe
exactly which profiles get killed without any real processes.
"""
import json
import subprocess
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
_SCRIPT = _ROOT / (
    "build/live-build/config/includes.chroot"
    "/opt/bluebird-kiosk/bin/launch-kiosk-independent"
)


def _run_preempt(tmp_path, outputs: dict, incident: int):
    """Source the script with stubbed incident + shimmed pgrep/pkill; run the preempt pass.
    Returns the set of --user-data-dir values passed to pkill (i.e. profiles killed)."""
    content = tmp_path / "display-content.json"
    content.write_text(json.dumps({"outputs": outputs}), encoding="utf-8")
    killlog = tmp_path / "killed.txt"
    shim = tmp_path / "bin"
    shim.mkdir()
    # pgrep: pretend a Chromium exists for every profile (exit 0).
    (shim / "pgrep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    # pkill: record the --user-data-dir=... arg it was asked to kill.
    (shim / "pkill").write_text(
        '#!/usr/bin/env bash\nfor a in "$@"; do case "$a" in --user-data-dir=*)'
        ' echo "$a" >> "$KILLLOG";; esac; done\nexit 0\n', encoding="utf-8")
    for f in ("pgrep", "pkill"):
        (shim / f).chmod(0o755)
    script = (
        'source "$1"\n'
        # override the live loopback poll with a fixed verdict
        f'_incident_active() {{ return {0 if incident else 1}; }}\n'
        'CONTENT="$2"\n'
        '_preempt_casts_on_incident\n'
    )
    subprocess.run(
        ["bash", "-c", script, "_", str(_SCRIPT), str(content)],
        capture_output=True, text=True, timeout=20,
        env={"PATH": f"{shim}:/usr/bin:/bin", "KILLLOG": str(killlog)},
    )
    if not killlog.exists():
        return set()
    return {ln.strip() for ln in killlog.read_text(encoding="utf-8").splitlines() if ln.strip()}


def _profile(output_name: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in output_name)
    return f"--user-data-dir=/var/lib/bluebird-kiosk/kiosk-chromium-{safe}"


def test_incident_kills_only_cast_outputs(tmp_path):
    killed = _run_preempt(tmp_path, {
        "DP-1": {"mode": "url", "url": "https://example.com/deck"},
        "HDMI-A-1": {"mode": "legacy_wall", "url": ""},
        "DP-2": {"mode": "signage", "url": "https://b/nen/beacon/d/KEY"},
    }, incident=1)
    assert killed == {_profile("DP-1")}   # only the cast output's foreign page is killed


def test_multiple_cast_outputs_all_preempted_up_front(tmp_path):
    killed = _run_preempt(tmp_path, {
        "DP-1": {"mode": "url", "url": "https://example.com/a"},
        "DP-2": {"mode": "url", "url": "https://example.com/b"},
    }, incident=1)
    # both cast screens are killed in the SAME pre-pass (not serialized behind a 40s relaunch)
    assert killed == {_profile("DP-1"), _profile("DP-2")}


def test_no_incident_no_kill(tmp_path):
    killed = _run_preempt(tmp_path, {
        "DP-1": {"mode": "url", "url": "https://example.com/deck"},
    }, incident=0)
    assert killed == set()   # nothing killed when no incident (and fail-safe: poll error == inactive)
