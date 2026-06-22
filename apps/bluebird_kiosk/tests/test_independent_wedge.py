"""Per-output wedged-page detection in launch-kiosk-independent.

The launcher is a bash script (the in-session sway exec for DISPLAY_LAYOUT=independent). It now
reloads ONLY the output whose Chromium window title looks like a server/browser error page — the
global page-activity watchdog can't see this on a multi-display kiosk because a healthy sibling
keeps the shared freshness clock fresh. We test the title heuristic directly by sourcing the
script (its reconcile loop is guarded so sourcing just loads the functions).
"""
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


def _is_wedged(title: str) -> bool:
    """Source the script and ask its _title_is_wedged whether <title> reads as an error page."""
    r = subprocess.run(
        ["bash", "-c", 'source "$1"; _title_is_wedged "$2"; echo "RC=$?"', "_", str(_SCRIPT), title],
        capture_output=True, text=True, timeout=20,
    )
    return "RC=0" in r.stdout


# ── error pages MUST be detected as wedged ────────────────────────────────────

def test_nginx_503_is_wedged():
    assert _is_wedged("503 Service Temporarily Unavailable")  # the exact NEN case


def test_cloudflare_errors_are_wedged():
    assert _is_wedged("bluebirdalerts.com | 502: Bad gateway")
    assert _is_wedged("bluebirdalerts.com | 521: Web server is down")
    assert _is_wedged("bluebirdalerts.com | 522: Connection timed out")


def test_gateway_timeout_is_wedged():
    assert _is_wedged("504 Gateway Time-out")


def test_browser_net_errors_are_wedged():
    assert _is_wedged("This site can’t be reached")
    assert _is_wedged("No internet")
    assert _is_wedged("bluebirdalerts.com took too long to respond")
    assert _is_wedged("ERR_CONNECTION_REFUSED")
    assert _is_wedged("Your connection is not private")


# ── real boards MUST NOT be flagged ───────────────────────────────────────────

def test_legacy_wall_title_not_wedged():
    assert not _is_wedged("Northeast Nodaway — Legacy Wall")


def test_beacon_titles_not_wedged():
    assert not _is_wedged("Main Lobby — Beacon")
    assert not _is_wedged("Bluejay Announcements — Beacon")


def test_empty_and_loading_titles_not_wedged():
    assert not _is_wedged("")
    assert not _is_wedged("Loading…")


def test_innocuous_board_text_not_wedged():
    # A board whose content merely mentions a room or number must not trip the heuristic.
    assert not _is_wedged("Room 503 — Chess Club")
    assert not _is_wedged("Gateway to Excellence — Beacon")
