"""Admin PIN verification + lockout state.

The PIN is a 6-digit numeric string. We store it as a bcrypt hash so a leak
of /etc/bluebird/admin.pin alone isn't enough to authenticate. After 3 wrong
attempts the in-memory lockout blocks new attempts for 5 minutes.

This is intentionally an in-memory lockout — a power cycle clears it, which
is fine: the kiosk's admin overlay is local-only, no remote brute-forcing
surface exists.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

import bcrypt

from .. import config


PIN_REGEX = re.compile(r"^\d{6}$")
LOCKOUT_AFTER_FAILURES = 3
LOCKOUT_SECONDS = 5 * 60


@dataclass
class LockoutState:
    failures: int = 0
    locked_until: float = 0.0
    failed_at: list = field(default_factory=list)


_state = LockoutState()


def set_pin(pin: str) -> None:
    if not PIN_REGEX.match(pin):
        raise ValueError("PIN must be exactly 6 digits.")
    hashed = bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt())
    config.set_admin_pin_hash(hashed)
    reset_lockout()


def has_pin() -> bool:
    return config.get_admin_pin_hash() is not None


def verify_pin(pin: str) -> bool:
    """Return True if the PIN matches. Increments lockout counter on failure."""
    if not PIN_REGEX.match(pin):
        _record_failure()
        return False
    if is_locked_out():
        return False
    stored = config.get_admin_pin_hash()
    if stored is None:
        return False
    try:
        ok = bcrypt.checkpw(pin.encode("utf-8"), stored)
    except ValueError:
        ok = False
    if ok:
        reset_lockout()
    else:
        _record_failure()
    return ok


def is_locked_out() -> bool:
    return time.monotonic() < _state.locked_until


def lockout_seconds_remaining() -> int:
    remaining = _state.locked_until - time.monotonic()
    return max(0, int(remaining))


def reset_lockout() -> None:
    _state.failures = 0
    _state.locked_until = 0.0
    _state.failed_at.clear()


def _record_failure() -> None:
    _state.failures += 1
    _state.failed_at.append(time.monotonic())
    if _state.failures >= LOCKOUT_AFTER_FAILURES:
        _state.locked_until = time.monotonic() + LOCKOUT_SECONDS


def admin_session_valid(token: Optional[str], known_tokens: dict) -> bool:
    if not token:
        return False
    exp = known_tokens.get(token)
    if exp is None:
        return False
    if time.monotonic() > exp:
        known_tokens.pop(token, None)
        return False
    return True
