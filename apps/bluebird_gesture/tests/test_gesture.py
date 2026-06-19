"""Tests for the gesture daemon's pure helpers (corner detection + sway-socket lookup).

evdev is a hardware lib not installed in CI, so we stub it in sys.modules before import —
the helpers under test don't touch evdev, only the watch loop does.
"""
import sys
from pathlib import Path
from unittest import mock

sys.modules.setdefault("evdev", mock.MagicMock())

_APPS = Path(__file__).resolve().parents[2]
if str(_APPS) not in sys.path:
    sys.path.insert(0, str(_APPS))

from bluebird_gesture import __main__ as g  # noqa: E402


# ── corner detection (1000x1000 panel, frac 0.15 => zones <150 or >850) ───────

def test_is_corner_all_four():
    for x, y in [(10, 10), (990, 10), (10, 990), (990, 990)]:
        assert g._is_corner(x, y, 1000, 1000), (x, y)


def test_is_corner_rejects_center_and_edge_midpoints():
    assert not g._is_corner(500, 500, 1000, 1000)   # dead center
    assert not g._is_corner(10, 500, 1000, 1000)    # left edge middle (not a corner)
    assert not g._is_corner(500, 10, 1000, 1000)    # top edge middle


def test_is_corner_rotation_agnostic():
    # whatever the rotation, a physical corner is still a corner -> all four accepted above,
    # and a near-corner just inside the zone still counts
    assert g._is_corner(149, 149, 1000, 1000)
    assert not g._is_corner(151, 151, 1000, 1000)


def test_is_corner_safe_on_unknown_inputs():
    assert not g._is_corner(None, None, 1000, 1000)
    assert not g._is_corner(10, 10, 0, 0)           # no panel range -> disabled


# ── sway socket lookup ────────────────────────────────────────────────────────

def test_sway_socket_unknown_user_is_none():
    with mock.patch.object(g.pwd, "getpwnam", side_effect=KeyError):
        assert g._sway_socket() is None


def test_sway_socket_picks_kiosk_uid_socket(tmp_path):
    fake = mock.Mock(); fake.pw_uid = 4242
    sock = "/run/user/4242/sway-ipc.4242.99.sock"
    with mock.patch.object(g.pwd, "getpwnam", return_value=fake), \
            mock.patch.object(g.glob, "glob", return_value=[sock]) as gl:
        assert g._sway_socket() == sock
        assert "4242" in gl.call_args[0][0]         # globbed the kiosk uid, not root's
