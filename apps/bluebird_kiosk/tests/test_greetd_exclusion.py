"""Guard the greetd/bluebird-kiosk mutual exclusion across BOTH install paths.

#36 (2026-06-24) removed `Conflicts=greetd` from bluebird-kiosk.service and replaced it with
condition-based exclusion:

    bluebird-kiosk.service   ConditionPathExists=/etc/bluebird/configured
    greetd.service drop-in   ConditionPathExists=!/etc/bluebird/configured

...but shipped the drop-in ONLY under build/live-build/. install/install.sh — the Ubuntu
autoinstall path every fielded kiosk actually uses — never wrote it, so on those boxes greetd
had no condition at all. install.sh enables greetd AND enables bluebird-gesture.service, whose
`Wants=bluebird-kiosk.service` drags the no-[Install] standby into every boot, so two full sway
sessions started and fought over DRM master: no frames reached any output while the heartbeat,
uptime and resource stats all kept reporting healthy. That took the NEN lobby kiosk down on
2026-08-05/06 and was invisible to the fleet console for a day.

These tests pin the invariant in both paths so the fix can't be lost again.
"""
from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
_LIVE_BUILD_DROPIN = _ROOT / (
    "build/live-build/config/includes.chroot/etc/systemd/system"
    "/greetd.service.d/10-bluebird-firstboot.conf"
)
_INSTALL_SH = _ROOT / "install/install.sh"
_KIOSK_UNIT = _ROOT / (
    "build/live-build/config/includes.chroot/etc/systemd/system/bluebird-kiosk.service"
)

_CONDITION = "ConditionPathExists=!/etc/bluebird/configured"


def test_live_build_ships_the_greetd_condition_dropin():
    assert _LIVE_BUILD_DROPIN.is_file(), "live-build lost the greetd exclusion drop-in"
    assert _CONDITION in _LIVE_BUILD_DROPIN.read_text(encoding="utf-8")


def test_install_sh_writes_the_greetd_condition_dropin():
    """The regression itself: install.sh must create the same drop-in the image ships."""
    body = _INSTALL_SH.read_text(encoding="utf-8")
    assert "/etc/systemd/system/greetd.service.d" in body, (
        "install.sh does not create the greetd drop-in directory — Ubuntu installs will run "
        "greetd AND bluebird-kiosk.service, giving two compositors fighting over DRM master"
    )
    assert _CONDITION in body, (
        "install.sh does not write ConditionPathExists=!/etc/bluebird/configured — greetd will "
        "start unconditionally on every boot alongside bluebird-kiosk.service"
    )


def test_kiosk_unit_carries_the_matching_positive_condition():
    """Half the exclusion lives on the other unit; without it the pair aren't exclusive."""
    body = _KIOSK_UNIT.read_text(encoding="utf-8")
    assert "ConditionPathExists=/etc/bluebird/configured" in body


def test_install_sh_does_not_mask_greetd():
    """Masking greetd is the tempting 'simpler' fix and it reintroduces #36.

    On a fresh install the marker doesn't exist yet, so bluebird-kiosk.service is
    condition-skipped. With greetd masked there would be NO compositor and the box sits at a
    text console — exactly the bug the condition design was introduced to fix.
    """
    body = _INSTALL_SH.read_text(encoding="utf-8")
    assert "systemctl mask greetd" not in body
    assert "disable greetd" not in body


def test_install_sh_heals_a_box_already_running_two_compositors():
    """The drop-in only takes effect at the next start, so an updated box would stay broken
    until it rebooted. install.sh stops greetd when the kiosk unit genuinely owns the session."""
    body = _INSTALL_SH.read_text(encoding="utf-8")
    assert "systemctl stop greetd.service" in body
    # ...but only when it is safe: configured marker present AND the kiosk unit active.
    assert "is-active --quiet bluebird-kiosk.service" in body
    assert "/etc/bluebird/configured" in body


# ── restart_kiosk must not create the fault it exists to clear ────────────────

def test_restart_kiosk_prefers_the_unit_that_is_actually_running(monkeypatch):
    """It used to restart greetd FIRST and fall back. On a configured box
    bluebird-kiosk.service owns the session (greetd is condition-skipped), so that started a
    SECOND compositor alongside the running one — the exact dual-compositor fault the button
    exists to clear. Field-confirmed during the 2026-08-06/07 NEN lobby outage."""
    import sys
    sys.path.insert(0, str(_ROOT / "apps"))
    from bluebird_kiosk.services import heartbeat

    calls = []

    def fake_systemctl(args, timeout_s=None):
        calls.append(list(args))
        if args[:2] == ["is-active", "--quiet"]:
            # kiosk unit active, greetd not — the normal configured-box state.
            return (args[2] == "bluebird-kiosk.service"), "ok"
        return True, "ok"

    monkeypatch.setattr(heartbeat, "_run_systemctl", fake_systemctl)
    ok, msg = heartbeat._cmd_restart_kiosk()
    assert ok
    assert "bluebird-kiosk.service" in msg
    assert not any(c[:1] == ["restart"] and c[1] == "greetd" for c in calls), \
        "must not restart greetd while bluebird-kiosk.service owns the session"


def test_restart_kiosk_heals_a_box_already_running_both(monkeypatch):
    """If both are somehow up, stop greetd rather than restarting either — restarting would
    just re-race two compositors for DRM master."""
    import sys
    sys.path.insert(0, str(_ROOT / "apps"))
    from bluebird_kiosk.services import heartbeat

    calls = []

    def fake_systemctl(args, timeout_s=None):
        calls.append(list(args))
        if args[:2] == ["is-active", "--quiet"]:
            return True, "ok"          # BOTH active
        return True, "ok"

    monkeypatch.setattr(heartbeat, "_run_systemctl", fake_systemctl)
    ok, msg = heartbeat._cmd_restart_kiosk()
    assert ok
    assert ["stop", "greetd"] in calls
    assert "two compositors" in msg
