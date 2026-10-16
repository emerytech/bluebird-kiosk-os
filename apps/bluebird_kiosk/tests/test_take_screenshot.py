"""take-screenshot (bash): the skip paths that keep a busy lobby board responsive.

The script is run for real, with its paths pointed at a temp tree (the BLUEBIRD_*
env overrides exist only for this). `logger` is shimmed onto PATH so journal lines
land in a file we can assert on. Every case stops before grim: the fake runtime dir
has no wayland socket, so a run that gets PAST the gates logs "no wayland socket" —
that line is our proof the capture path was taken.
"""
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
_BIN = _ROOT / "build/live-build/config/includes.chroot/opt/bluebird-kiosk/bin"
_SCRIPT = _BIN / "take-screenshot"
_HELPER = _BIN / "screenshot-idle-check"

CAPTURE_PATH_MARK = "no wayland socket"          # logged only after every skip gate passed


@pytest.fixture
def kiosk(tmp_path):
    conf = tmp_path / "kiosk.conf"
    conf.write_text('BLUEBIRD_BACKEND="http://127.0.0.1:9"\n', encoding="utf-8")
    token = tmp_path / "license.token"
    token.write_text("test-token\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    runtime = tmp_path / "run"                     # deliberately holds no wayland-* socket
    runtime.mkdir()
    shim = tmp_path / "shim"
    shim.mkdir()
    journal = tmp_path / "journal.log"
    logger = shim / "logger"
    logger.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$LOGGER_OUT"\n', encoding="utf-8")
    logger.chmod(logger.stat().st_mode | stat.S_IEXEC)

    env = dict(
        os.environ,
        PATH="%s:%s" % (shim, os.environ.get("PATH", "")),
        LOGGER_OUT=str(journal),
        BLUEBIRD_KIOSK_CONF=str(conf),
        BLUEBIRD_LICENSE_TOKEN_FILE=str(token),
        BLUEBIRD_STATE_DIR=str(state),
        BLUEBIRD_RUNTIME_DIR=str(runtime),
    )

    def run(script=_SCRIPT):
        proc = subprocess.run(["bash", str(script)], env=env, capture_output=True,
                              text=True, timeout=30)
        log = journal.read_text(encoding="utf-8") if journal.exists() else ""
        return proc.returncode, log

    def touch(age_secs):
        (state / "last_input.json").write_text(
            json.dumps({"last_input": time.time() - age_secs}), encoding="utf-8")

    def uploaded(age_secs):
        p = state / "screenshot_last_upload"
        p.touch()
        os.utime(p, (time.time() - age_secs, time.time() - age_secs))

    return SimpleNamespace(run=run, touch=touch, uploaded=uploaded, state=state,
                           conf=conf, tmp=tmp_path)


def test_recent_touch_skips_the_capture(kiosk):
    kiosk.touch(5)
    rc, log = kiosk.run()
    assert rc == 0
    assert "skipping screenshot: touch input 5s ago" in log
    assert CAPTURE_PATH_MARK not in log
    assert (kiosk.state / "screenshot_last_upload").exists()      # defer budget started


def test_idle_touchscreen_captures(kiosk):
    kiosk.touch(120)
    rc, log = kiosk.run()
    assert rc == 0
    assert CAPTURE_PATH_MARK in log and "skipping screenshot" not in log


def test_no_touch_stamp_captures_as_before(kiosk):
    # Older gesture daemon / no touchscreen: nothing changes for that kiosk.
    rc, log = kiosk.run()
    assert rc == 0 and CAPTURE_PATH_MARK in log


def test_defer_budget_forces_a_capture_on_a_busy_board(kiosk):
    kiosk.touch(5)
    kiosk.uploaded(700)                                            # > SCREENSHOT_MAX_DEFER_SECS
    rc, log = kiosk.run()
    assert rc == 0 and CAPTURE_PATH_MARK in log


def test_scheduled_off_skip_is_still_first(kiosk):
    (kiosk.state / "power_state.json").write_text(
        json.dumps({"display_on": False, "scheduled_off": True, "reason": "schedule"}),
        encoding="utf-8")
    kiosk.touch(120)
    rc, log = kiosk.run()
    assert rc == 0 and "display scheduled off" in log and CAPTURE_PATH_MARK not in log


def test_missing_helper_fails_open_to_capture(kiosk):
    # A partially-updated kiosk (script without the helper beside it) must capture.
    alone = kiosk.tmp / "bin-without-helper"
    alone.mkdir()
    shutil.copy(_SCRIPT, alone / "take-screenshot")
    kiosk.touch(5)
    rc, log = kiosk.run(alone / "take-screenshot")
    assert rc == 0 and CAPTURE_PATH_MARK in log


def test_kiosk_conf_can_tune_the_idle_window(kiosk):
    kiosk.conf.write_text(
        'BLUEBIRD_BACKEND="http://127.0.0.1:9"\nSCREENSHOT_IDLE_SECS=2\n', encoding="utf-8")
    kiosk.touch(5)
    rc, log = kiosk.run()
    assert rc == 0 and CAPTURE_PATH_MARK in log


def test_operator_request_bypasses_the_idle_gate(kiosk):
    # Fleet-console "Screenshot now": heartbeat stages screenshot.request, then starts
    # the unit. The operator asks because someone is AT the board — never defer that.
    kiosk.touch(5)
    (kiosk.state / "screenshot.request").write_text("", encoding="utf-8")
    rc, log = kiosk.run()
    assert rc == 0 and CAPTURE_PATH_MARK in log
    assert "idle gate bypassed" in log and "skipping screenshot" not in log
    assert not (kiosk.state / "screenshot.request").exists()        # consumed, never sticky


def test_stale_request_is_consumed_even_when_an_earlier_gate_exits(kiosk):
    # A request that arrives while the panel is scheduled-off must not linger and force
    # the NEXT periodic tick after the display comes back.
    (kiosk.state / "power_state.json").write_text(
        json.dumps({"display_on": False, "scheduled_off": True, "reason": "schedule"}),
        encoding="utf-8")
    (kiosk.state / "screenshot.request").write_text("", encoding="utf-8")
    rc, log = kiosk.run()
    assert rc == 0 and "display scheduled off" in log and CAPTURE_PATH_MARK not in log
    assert not (kiosk.state / "screenshot.request").exists()


def test_grim_is_invoked_downscaled_with_fast_png():
    src = _SCRIPT.read_text(encoding="utf-8")
    assert 'grim -s "$SCREENSHOT_SCALE" -l "$SCREENSHOT_PNG_LEVEL" -t png -' in src
    assert 'SCREENSHOT_SCALE="${SCREENSHOT_SCALE:-0.5}"' in src
    assert 'SCREENSHOT_PNG_LEVEL="${SCREENSHOT_PNG_LEVEL:-1}"' in src
    assert "-t jpeg" not in src          # backend accepts image/png only (415 otherwise)
    # `rc=$?` inside `if ! cmd; then` is the negated test (always 0), so the grim-timeout
    # branch could never fire. The exit status must be captured directly after grim.
    assert "if ! timeout" not in src
    assert src.index('-t png - > "$TMP_PNG"') < src.index('rc=$?\nif [[ "$rc" -ne 0 ]]; then')


def test_helper_ships_beside_the_script_and_is_executable():
    assert _HELPER.exists()
    assert os.access(_HELPER, os.X_OK)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_take_screenshot_is_shellcheck_clean():
    proc = subprocess.run(["shellcheck", str(_SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
