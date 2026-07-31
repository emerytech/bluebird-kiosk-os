"""Operator-chosen password reset — the privilege split and what crosses the wire.

A locked-out operator previously had one option: reinstall, which wipes the
box's display settings and media cache and takes a live board down. The reset
path exists to avoid that.

The operator types the password in the super-admin console, but the SERVER
hashes it before it is stored or sent, so what arrives here is a crypt(3)
record — never the password. The properties worth pinning:

  * the heartbeat (unprivileged, NoNewPrivileges) must NOT apply it itself
  * a value that is not a valid crypt record never reaches chpasswd
  * the staged request file is removed on every path, including failures
  * chpasswd is invoked with -e (already hashed) and never via argv
"""
from __future__ import annotations

import configparser
import json
import os
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

# A real crypt(3) SHA-512 record shape: $6$<salt>$<86-char checksum>.
_GOOD = "$6$abcdefghijklmnop$" + ("a" * 86)


# ── the privilege split ──────────────────────────────────────────────────────

def test_heartbeat_stays_unprivileged():
    # If this gains User=root or drops NoNewPrivileges, the reason for the
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
    install = dict(cp.items("Install")) if cp.has_section("Install") else {}
    assert not install.get("WantedBy")
    assert not (_UNIT.parent / "bluebird-reset-access.timer").exists()
    assert svc.get("ReadWritePaths") == "/var/lib/bluebird-kiosk"
    assert svc.get("PrivateNetwork") == "yes"


def test_script_uses_prehashed_chpasswd_and_never_argv():
    body = _SCRIPT.read_text(encoding="utf-8")
    # -e means the value is already a crypt record; without it chpasswd would
    # treat the hash as a literal password.
    assert "chpasswd -e" in body
    # Piped, not passed as an argument — argv is world-readable via /proc.
    assert "| chpasswd -e" in body
    assert "set -euo pipefail" in body
    # The staged credential must be cleaned up however the script exits.
    assert "trap cleanup EXIT" in body


# ── the script's input surface ───────────────────────────────────────────────

def _run_script(tmp_path, payload, *, write_raw=None):
    """Run the real script with chpasswd/passwd/id stubbed, and return what it
    would have applied."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    captured = tmp_path / "chpasswd.txt"
    (fakebin / "chpasswd").write_text(
        "#!/usr/bin/env bash\ncat > %s\n" % captured, encoding="utf-8")
    for stub in ("passwd", "id"):
        (fakebin / stub).write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for f in fakebin.iterdir():
        f.chmod(0o755)

    request = tmp_path / "access-reset.request"
    if write_raw is not None:
        request.write_text(write_raw, encoding="utf-8")
    elif payload is not None:
        request.write_text(json.dumps(payload), encoding="utf-8")

    body = _SCRIPT.read_text(encoding="utf-8").replace(
        "REQUEST=/var/lib/bluebird-kiosk/access-reset.request",
        "REQUEST=%s" % request,
    )
    script = tmp_path / "reset-access"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)

    env = dict(os.environ, PATH="%s:%s" % (fakebin, os.environ.get("PATH", "")))
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=env, timeout=30)
    applied = captured.read_text(encoding="utf-8") if captured.exists() else ""
    return proc, applied, request


@pytest.mark.skipif(os.name != "posix", reason="shell script test")
def test_applies_a_valid_hash(tmp_path):
    proc, applied, request = _run_script(tmp_path, {"shadow": _GOOD})
    assert proc.returncode == 0, proc.stderr
    assert applied.strip() == "bluebird:%s" % _GOOD
    # Credential material must not survive the run.
    assert not request.exists()


@pytest.mark.skipif(os.name != "posix", reason="shell script test")
@pytest.mark.parametrize("bad", [
    "hunter2",                              # plaintext — must never be applied
    "$1$abc$short",                         # MD5 crypt, not SHA-512
    "$6$salt$tooshort",                     # right prefix, wrong length
    "$6$abcdefghijklmnop$" + ("a" * 85),    # off by one
    "$6$abc$%s; rm -rf /" % ("a" * 86),     # shell metacharacters
])
def test_rejects_anything_that_is_not_a_crypt_record(tmp_path, bad):
    proc, applied, request = _run_script(tmp_path, {"shadow": bad})
    assert proc.returncode != 0, "accepted a non-crypt value: %r" % bad
    assert applied == "", "passed a non-crypt value to chpasswd: %r" % bad
    assert not request.exists()


@pytest.mark.skipif(os.name != "posix", reason="shell script test")
def test_missing_and_malformed_requests_are_refused(tmp_path):
    proc, applied, _ = _run_script(tmp_path, None)          # no request at all
    assert proc.returncode == 2 and applied == ""
    proc, applied, req = _run_script(tmp_path, None, write_raw="not json")
    assert proc.returncode != 0 and applied == ""
    assert not req.exists()


# ── the heartbeat half ───────────────────────────────────────────────────────

@pytest.fixture
def hb(monkeypatch, tmp_path):
    from bluebird_kiosk.services import heartbeat  # noqa: WPS433
    monkeypatch.setattr(heartbeat, "_ACCESS_REQUEST", tmp_path / "access-reset.request")
    return heartbeat


def test_reset_access_takes_args(hb):
    assert "reset_access" in hb._COMMAND_EXECUTORS
    assert "reset_access" in hb._COMMANDS_WITH_ARGS


def test_stages_the_hash_and_starts_the_unit(hb, monkeypatch, tmp_path):
    req = tmp_path / "access-reset.request"
    seen = {}

    def fake_systemctl(argv, timeout_s=30):
        assert argv == ["start", "bluebird-reset-access.service"]
        seen["staged"] = json.loads(req.read_text(encoding="utf-8"))
        seen["mode"] = oct(req.stat().st_mode & 0o777)
        return True, "ok"

    monkeypatch.setattr(hb, "_run_systemctl", fake_systemctl)
    ok, msg = hb._cmd_reset_access({"shadow": _GOOD})
    assert ok, msg
    assert seen["staged"] == {"shadow": _GOOD}
    assert seen["mode"] == "0o600", "staged credential was not 0600"
    assert not req.exists(), "staged credential survived the run"


def test_rejects_a_bad_hash_without_touching_the_unit(hb, monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(hb, "_run_systemctl",
                        lambda argv, timeout_s=30: called.append(argv) or (True, "ok"))
    ok, msg = hb._cmd_reset_access({"shadow": "hunter2"})
    assert not ok
    assert not called, "started the privileged unit with an invalid hash"
    assert not (tmp_path / "access-reset.request").exists()


def test_missing_payload_is_an_error(hb, monkeypatch):
    monkeypatch.setattr(hb, "_run_systemctl",
                        lambda argv, timeout_s=30: (True, "ok"))
    ok, msg = hb._cmd_reset_access({})
    assert not ok and "no password" in msg


def test_request_is_removed_even_when_the_unit_fails(hb, monkeypatch, tmp_path):
    req = tmp_path / "access-reset.request"
    monkeypatch.setattr(hb, "_run_systemctl",
                        lambda argv, timeout_s=30: (False, "unit failed"))
    ok, _ = hb._cmd_reset_access({"shadow": _GOOD})
    assert not ok
    assert not req.exists(), "a credential file survived a failed run"


def test_result_never_echoes_the_hash(hb, monkeypatch):
    monkeypatch.setattr(hb, "_run_systemctl", lambda argv, timeout_s=30: (True, "ok"))
    ok, msg = hb._cmd_reset_access({"shadow": _GOOD})
    assert ok
    # The result is stored server-side and shown in the console history.
    assert _GOOD not in msg
