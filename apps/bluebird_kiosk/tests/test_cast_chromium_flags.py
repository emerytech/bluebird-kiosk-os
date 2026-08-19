"""Security regression guard: a CAST Chromium window (KIOSK_OUTPUT_CAST=1) must keep Chromium's
Private/Local Network Access protections ON. A cast is untrusted third-party JS; with PNA/LNA
disabled it could reach the kiosk's own loopback admin server (127.0.0.1:7311, which has an
unauthenticated open-overlay endpoint). First-party pages (Legacy Wall / Beacon) still disable
PNA because they need the localhost image-cache shim; casts don't.

This is a static guard on the launcher source — the script execs Chromium, so we assert on the
flag-selection logic rather than launching a browser.
"""
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = next(p for p in _HERE.parents if (p / "build").is_dir() and (p / "apps").is_dir())
_SCRIPT = (_ROOT / "build/live-build/config/includes.chroot"
                   "/opt/bluebird-kiosk/bin/launch-kiosk-chromium").read_text(encoding="utf-8")

_PNA_TOKENS = (
    "BlockInsecurePrivateNetworkRequests",
    "PrivateNetworkAccessSendPreflights",
    "PrivateNetworkAccessRespectPreflightResults",
    "LocalNetworkAccessChecks",
    "LocalNetworkAccess",
)


def _cast_branch() -> str:
    """The body of the flag-selection `if [[ "${KIOSK_OUTPUT_CAST:-}" == "1" ]]; then ... fi`
    block. The token also appears in the earlier URL-arming block, so anchor on the LAST
    occurrence (the flag block sits just above the chromium exec)."""
    marker = 'KIOSK_OUTPUT_CAST:-}" == "1" ]]; then'
    i = _SCRIPT.rindex(marker)
    j = _SCRIPT.index("\nfi", i)
    return _SCRIPT[i:j]


def test_cast_branch_reenables_private_network_protection():
    branch = _cast_branch()
    # the cast branch narrows DISABLE_FEATURES; it must NOT re-list any PNA/LNA disable token
    assert 'DISABLE_FEATURES=' in branch
    for tok in _PNA_TOKENS:
        assert tok not in branch, f"cast branch must not disable {tok}"


def test_cast_branch_drops_insecure_content_and_extension():
    branch = _cast_branch()
    assert 'INSECURE_FLAG=""' in branch      # no --allow-running-insecure-content for a cast
    assert 'EXT_FLAG=""' in branch           # no cache extension loaded into a foreign page


def test_exec_uses_variable_not_hardcoded_pna_list():
    # the chromium exec must consume the computed $DISABLE_FEATURES / $INSECURE_FLAG, otherwise
    # the cast branch's narrowing would be dead code and PNA would stay disabled for casts.
    assert '--disable-features="$DISABLE_FEATURES"' in _SCRIPT
    assert "${INSECURE_FLAG}" in _SCRIPT
    # and the old hardcoded PNA-disabling --disable-features line must be gone
    assert "--disable-features=TranslateUI,BlockInsecurePrivateNetworkRequests" not in _SCRIPT


def test_first_party_path_still_disables_pna():
    # regression the OTHER way: non-cast (Legacy Wall / Beacon) MUST still disable PNA so the
    # localhost image-cache shim keeps working — the default DISABLE_FEATURES carries the tokens.
    assert 'DISABLE_FEATURES="TranslateUI,BlockInsecurePrivateNetworkRequests' in _SCRIPT
