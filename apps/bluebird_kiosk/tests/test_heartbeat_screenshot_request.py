"""heartbeat: fleet-console "Screenshot now" stages a request file so take-screenshot
bypasses its touch-idle gate for that one frame (the consuming side is covered in
test_take_screenshot.py). Staging is best-effort — the unit must start either way.
"""
import sys
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
sys.path.insert(0, str(_ROOT / "apps"))

from bluebird_kiosk.services import heartbeat as hb  # noqa: E402


def test_take_screenshot_stages_a_request_before_starting_the_unit(tmp_path):
    req = tmp_path / "screenshot.request"
    calls = []

    def fake_systemctl(argv, timeout_s=30):
        calls.append((list(argv), timeout_s, req.exists()))   # must exist BEFORE the unit runs
        return True, "ok"

    with mock.patch.object(hb, "_SCREENSHOT_REQUEST", req), \
            mock.patch.object(hb, "_run_systemctl", side_effect=fake_systemctl):
        assert hb._cmd_take_screenshot() == (True, "ok")
    assert calls == [(["start", "bluebird-screenshot.service"], 60, True)]


def test_take_screenshot_still_runs_when_the_request_cannot_be_staged(tmp_path):
    unwritable = tmp_path / "no-such-dir" / "screenshot.request"      # parent does not exist
    with mock.patch.object(hb, "_SCREENSHOT_REQUEST", unwritable), \
            mock.patch.object(hb, "_run_systemctl", return_value=(True, "ok")) as run:
        assert hb._cmd_take_screenshot() == (True, "ok")
    run.assert_called_once_with(["start", "bluebird-screenshot.service"], timeout_s=60)
    assert not unwritable.exists()
