"""Remote access reset — the privilege split and the credential's lifetime.

A locked-out operator previously had one option: reinstall, which wipes the
box's display settings and media cache and takes a live board down. The reset
path exists to avoid that, but it is the only command whose result is a
credential, so the properties below are the ones worth pinning:

  * the heartbeat (unprivileged, NoNewPrivileges) must NOT do the work itself
  * the password is generated on the device — nothing is ever sent toward it
  * the handoff file is removed even when the run fails
  * the privileged script refuses any account outside its two-value allowlist
"""
from __future__ import annotations

import configparser
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
sys.path.insert(0, str(_ROOT / "apps"))

_INC = _ROOT / "build/live-build/config/includes.chroot"
_UNIT = _INC / "etc/systemd/system/bluebird-reset-access.service"
_SCRIPT = _INC / "opt/bluebird-kiosk/bin/reset-access"


# ── the privilege split ──────────────────────────────────────────────────────

def test_heartbeat_stays_unprivileged():
    # If this ever gains User=root or drops NoNewPrivileges, the reason for the
    # separate unit is gone and someone has widened the network-facing surface.
    cp = configparser.RawConfigParser(strict=False)
    cp.optionxform = str
    cp.read(_INC / "etc/systemd/system/bluebird-heartbeat.service", encoding="utf-8")
    svc = dict(cp.items("Service"))
    assert svc.get("User") == "bluebird-kiosk"
    assert svc.get("NoNewPrivileges") == "yes"


def test_reset_unit_is_on_demand_only():
    cp = configparser.RawConfigParser(strict=False)
    cp.optionxform = str
    cp.read(_UNIT, encoding="utf-8")
    svc = dict(cp.items("Service"))
    assert svc.get("Type") == "oneshot"
    # Nothing may start this on a schedule or at boot.
    install = dict(cp.items("Install")) if cp.has_section("Install") else {}
    assert not install.get("WantedBy")
    assert not (_UNIT.parent / "bluebird-reset-access.timer").exists()
    # It only needs to write the handoff.
    assert svc.get("ReadWritePaths") == "/var/lib/bluebird-kiosk"
    # It has no reason to touch the network.
    assert svc.get("PrivateNetwork") == "yes"


# ── the script's input surface ───────────────────────────────────────────────

def test_script_allowlists_the_target_account():
    body = _SCRIPT.read_text(encoding="utf-8")
    # Reachable from a network-delivered command: the account must never be
    # passed through to chpasswd unchecked.
    assert "bluebird|root)" in body
    assert "set -euo pipefail" in body


@pytest.mark.skipif(os.name != "posix", reason="shell script test")
def test_script_refuses_an_unknown_account():
    proc = subprocess.run(
        ["bash", str(_SCRIPT), "nobody-in-particular"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2, proc.stderr
    assert "refusing unknown target account" in proc.stderr


@pytest.mark.skipif(os.name != "posix", reason="shell script test")
def test_script_generates_a_strong_local_password(tmp_path, monkeypatch):
    """Run the real script with chpasswd/passwd stubbed, and inspect what it
    would have applied. Proves the password is device-generated, long, from the
    unambiguous alphabet, and written 0640 — not world-readable."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    captured = tmp_path / "chpasswd.txt"
    (fakebin / "chpasswd").write_text(
        "#!/usr/bin/env bash\ncat > %s\n" % captured, encoding="utf-8")
    (fakebin / "passwd").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (fakebin / "id").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (fakebin / "chown").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for f in fakebin.iterdir():
        f.chmod(0o755)

    handoff = tmp_path / "access-reset.json"
    body = _SCRIPT.read_text(encoding="utf-8").replace(
        "HANDOFF=/var/lib/bluebird-kiosk/access-reset.json",
        "HANDOFF=%s" % handoff,
    )
    script = tmp_path / "reset-access"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)

    env = dict(os.environ, PATH="%s:%s" % (fakebin, os.environ.get("PATH", "")))
    proc = subprocess.run(["bash", str(script), "bluebird"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr

    applied = captured.read_text(encoding="utf-8").strip()
    account, _, password = applied.partition(":")
    assert account == "bluebird"
    assert len(password) == 24
    # Unambiguous alphabet — this gets read off a screen and typed by hand.
    assert re.fullmatch(r"[A-HJ-NP-Za-km-z2-9]{24}", password), password

    payload = json.loads(handoff.read_text(encoding="utf-8"))
    assert payload["password"] == password
    assert payload["account"] == "bluebird"
    mode = stat.S_IMODE(handoff.stat().st_mode)
    assert mode == 0o640, oct(mode)

    # Two runs must not produce the same credential.
    subprocess.run(["bash", str(script), "bluebird"],
                   capture_output=True, text=True, env=env, timeout=30, check=True)
    assert captured.read_text(encoding="utf-8").strip() != applied


# ── the heartbeat half ───────────────────────────────────────────────────────

@pytest.fixture
def hb(monkeypatch, tmp_path):
    from bluebird_kiosk.services import heartbeat  # noqa: WPS433
    monkeypatch.setattr(heartbeat, "_ACCESS_HANDOFF", tmp_path / "access-reset.json")
    return heartbeat


def test_reset_access_is_registered(hb):
    assert "reset_access" in hb._COMMAND_EXECUTORS


def test_reset_access_reports_the_generated_password(hb, monkeypatch, tmp_path):
    handoff = tmp_path / "access-reset.json"

    def fake_systemctl(argv, timeout_s=30):
        assert argv == ["start", "bluebird-reset-access.service"]
        handoff.write_text(json.dumps(
            {"account": "bluebird", "password": "SwiftHarborMoon7429xy"}), encoding="utf-8")
        return True, "ok"

    monkeypatch.setattr(hb, "_run_systemctl", fake_systemctl)
    ok, msg = hb._cmd_reset_access()
    assert ok
    assert "SwiftHarborMoon7429xy" in msg
    assert "account=bluebird" in msg
    # The credential must not be left on disk after it has been reported.
    assert not handoff.exists()


def test_reset_access_removes_handoff_even_on_garbage(hb, monkeypatch, tmp_path):
    handoff = tmp_path / "access-reset.json"

    def fake_systemctl(argv, timeout_s=30):
        handoff.write_text("not json", encoding="utf-8")
        return True, "ok"

    monkeypatch.setattr(hb, "_run_systemctl", fake_systemctl)
    ok, msg = hb._cmd_reset_access()
    assert not ok
    assert not handoff.exists(), "a credential file survived a failed run"


def test_reset_access_does_not_report_a_stale_password(hb, monkeypatch, tmp_path):
    """If the unit fails to run, a leftover handoff from an earlier reset must
    never be reported as if it were the new credential."""
    handoff = tmp_path / "access-reset.json"
    handoff.write_text(json.dumps({"account": "bluebird", "password": "StaleOldValue"}),
                       encoding="utf-8")
    monkeypatch.setattr(hb, "_run_systemctl",
                        lambda argv, timeout_s=30: (False, "unit failed"))
    ok, msg = hb._cmd_reset_access()
    assert not ok
    assert "StaleOldValue" not in msg
