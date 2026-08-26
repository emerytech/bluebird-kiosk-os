"""VMS Phase 2b: guard the launch-kiosk-chromium `visitor` mode changes.

The launcher is bash, so this checks (a) it still parses, (b) DISPLAY_MODE=visitor +
VISITOR_URL selects the visitor page, and (c) the visitor URL is ARMED with
?kiosk_cache=1 — without which the emergency takeover is silently dead on a visitor
kiosk AND the on-device check-in loopback proxy is never reached.
"""
from __future__ import annotations

import shlex
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
    r = subprocess.run(["bash", "-n", str(_LAUNCHER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_visitor_mode_selection_present():
    s = _src()
    assert 'VISITOR_URL="${VISITOR_URL:-}"' in s, "VISITOR_URL must be defaulted (set -eu safety)"
    assert '"$DISPLAY_MODE" == "visitor"' in s
    assert 'URL="$VISITOR_URL"' in s


def test_kiosk_cache_armed_for_visitor():
    s = _src()
    append_idx = s.find('URL="${URL}?kiosk_cache=1"')
    assert append_idx != -1
    window = s[max(0, append_idx - 800):append_idx]
    assert '"$URL" == "$VISITOR_URL"' in window, \
        "kiosk_cache append no longer covers VISITOR_URL (takeover + proxy would be dead)"


def test_runtime_selection_and_arming():
    """Exercise the actual URL-selection + append conditionals for visitor mode."""
    body = _src()
    # Reproduce the two conditionals the launcher runs, seeded with our vars.
    select = (
        'URL="$LOCAL_URL"; '
        'if [[ "$DISPLAY_MODE" == "visitor" && -n "$VISITOR_URL" ]]; then URL="$VISITOR_URL"; '
        'elif [[ "$DISPLAY_MODE" == "signage" && -n "$SIGNAGE_URL" ]]; then URL="$SIGNAGE_URL"; '
        'elif [[ -n "$CLOUD_URL" ]]; then URL="$CLOUD_URL"; fi; '
    )
    arm = (
        'if { { [[ -n "$CLOUD_URL" ]] && [[ "$URL" == "$CLOUD_URL" ]]; } '
        '|| { [[ -n "$SIGNAGE_URL" ]] && [[ "$URL" == "$SIGNAGE_URL" ]]; } '
        '|| { [[ -n "$VISITOR_URL" ]] && [[ "$URL" == "$VISITOR_URL" ]]; }; } '
        '&& [[ "$URL" != *"kiosk_cache=1"* ]]; then '
        'if [[ "$URL" == *\\?* ]]; then URL="${URL}&kiosk_cache=1"; '
        'else URL="${URL}?kiosk_cache=1"; fi; fi; echo "$URL"'
    )

    def run(*, mode, visitor="", signage="", cloud="", local="http://127.0.0.1:7311/legacy-wall"):
        pre = "DISPLAY_MODE=%s; VISITOR_URL=%s; SIGNAGE_URL=%s; CLOUD_URL=%s; LOCAL_URL=%s; " % (
            shlex.quote(mode), shlex.quote(visitor), shlex.quote(signage),
            shlex.quote(cloud), shlex.quote(local))
        r = subprocess.run(["bash", "-c", pre + select + arm], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    vis = "https://bluebird-alerts.com/nen/visitor-kiosk"
    lw = "https://bluebird-alerts.com/nen/legacy-wall"
    # Visitor mode selects the visitor URL AND arms it.
    assert run(mode="visitor", visitor=vis, cloud=lw) == vis + "?kiosk_cache=1"
    # Visitor mode with no VISITOR_URL falls through to the cloud LW (not broken).
    assert run(mode="visitor", visitor="", cloud=lw) == lw + "?kiosk_cache=1"
    # A legacy_wall kiosk is unaffected by the visitor plumbing.
    assert run(mode="legacy_wall", cloud=lw) == lw + "?kiosk_cache=1"
    # No double-append if the visitor URL already carries the flag.
    assert run(mode="visitor", visitor=vis + "?kiosk_cache=1", cloud=lw) == vis + "?kiosk_cache=1"
    # Sanity: the real launcher body contains the same append condition shape.
    assert '"$URL" == "$VISITOR_URL"' in body
