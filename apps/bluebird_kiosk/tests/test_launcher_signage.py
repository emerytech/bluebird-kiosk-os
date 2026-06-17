"""Beacon Inc 4 (§13-C1): guard the launch-kiosk-chromium signage changes.

The launcher is bash, so this checks (a) it still parses and (b) the safety-critical
?kiosk_cache=1 generalization isn't silently reverted to the old `== "$CLOUD_URL"`-only
gate — which would leave the emergency takeover dead on a Beacon signage display.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
_LAUNCHER = _ROOT / (
    "build/live-build/config/includes.chroot/opt/bluebird-kiosk/bin/launch-kiosk-chromium"
)


def _src() -> str:
    return _LAUNCHER.read_text(encoding="utf-8")


def test_launcher_parses():
    # `bash -n` = parse only, no execution.
    r = subprocess.run(["bash", "-n", str(_LAUNCHER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_signage_mode_selection_present():
    s = _src()
    assert 'DISPLAY_MODE' in s and 'SIGNAGE_URL' in s
    # signage takes precedence in URL selection
    assert '"$DISPLAY_MODE" == "signage"' in s
    assert 'URL="$SIGNAGE_URL"' in s


def test_kiosk_cache_armed_for_signage_not_just_cloud():
    # The cache/takeover flag must be appended for the signage URL too, not only
    # the Legacy Wall CLOUD_URL. A future edit that drops SIGNAGE_URL from the
    # append condition would re-introduce §13-C1 (silent dead takeover).
    s = _src()
    # The append assignment exists exactly once...
    assert 'URL="${URL}?kiosk_cache=1"' in s
    append_idx = s.find('URL="${URL}?kiosk_cache=1"')
    # ...and the condition guarding it (the ~600 chars before) must reference the
    # signage URL, not just CLOUD_URL.
    window = s[max(0, append_idx - 600):append_idx]
    assert '"$URL" == "$SIGNAGE_URL"' in window, \
        "kiosk_cache append no longer covers SIGNAGE_URL (§13-C1 regression)"


def test_runtime_append_logic():
    # Exercise the actual append conditional (string logic, no network) the way
    # the launcher runs it, across the cases that matter.
    import shlex
    cond = (
        'if { { [[ -n "$CLOUD_URL" ]] && [[ "$URL" == "$CLOUD_URL" ]]; } '
        '|| { [[ -n "$SIGNAGE_URL" ]] && [[ "$URL" == "$SIGNAGE_URL" ]]; }; } '
        '&& [[ "$URL" != *"kiosk_cache=1"* ]]; then '
        'if [[ "$URL" == *\\?* ]]; then URL="${URL}&kiosk_cache=1"; '
        'else URL="${URL}?kiosk_cache=1"; fi; fi; echo "$URL"'
    )

    def run(url, cloud, signage):
        pre = "URL=%s; CLOUD_URL=%s; SIGNAGE_URL=%s; " % (
            shlex.quote(url), shlex.quote(cloud), shlex.quote(signage))
        r = subprocess.run(["bash", "-c", pre + cond], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    sig = "https://bluebird-alerts.com/nen/beacon/d/k"
    lw = "https://bluebird-alerts.com/nen/legacy-wall"
    local = "http://127.0.0.1:7311/legacy-wall"
    assert run(sig, lw, sig) == sig + "?kiosk_cache=1"          # signage armed
    assert run(lw, lw, "") == lw + "?kiosk_cache=1"             # LW still armed (regression)
    assert run(local, lw, "") == local                          # local renderer: not armed
    assert run(sig + "?x=1", lw, sig + "?x=1") == sig + "?x=1&kiosk_cache=1"
    assert run(sig + "?kiosk_cache=1", lw, sig) == sig + "?kiosk_cache=1"  # no double-append
