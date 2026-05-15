"""Cache + access to the super-admin-defined diagnostic allow-list.

The list lives in `/var/lib/bluebird-kiosk/diagnostics.json`, refreshed
periodically by the sync client. The Console tab UI renders one button
per cached entry; the kiosk-side runner validates id presence + regex
guard before executing.

The regex guard is intentionally duplicated from the backend — keeping
the file self-contained means the kiosk doesn't have to import server
code, and a stale cache from before a server-side guard bug is still
protected.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("bluebird-kiosk.diagnostics")


DIAGNOSTICS_CACHE_PATH = Path(
    os.environ.get(
        "BLUEBIRD_DIAGNOSTICS_CACHE",
        "/var/lib/bluebird-kiosk/diagnostics.json",
    )
)


# ── Regex guard (mirrors backend/app/services/kiosk_diagnostic_store.py) ─────


_DESTRUCTIVE_PATTERNS = [
    (re.compile(r"\brm\s+-[a-zA-Z]*[rfRF]", re.I), "rm -r/-f"),
    (re.compile(r"\brm\s+(?:[a-zA-Z-]+\s+)*[/]", re.I), "rm with absolute path"),
    (re.compile(r"\bdd\s+if=", re.I), "dd raw write"),
    (re.compile(r"\b(?:mkfs|fdisk|parted|wipefs|shred|fsck)\b", re.I),
     "filesystem management"),
    (re.compile(r"\b(?:chmod|chown|chgrp)\b", re.I), "permission change"),
    (re.compile(r"\b(?:sudo|su)\b\s+(?:-|[a-zA-Z])", re.I), "privilege escalation"),
    (re.compile(r"\b(?:passwd|useradd|userdel|usermod|groupadd|groupdel)\b", re.I),
     "account management"),
    (re.compile(r"\|\s*(?:bash|sh|zsh|fish|python\d*|perl|ruby|node|tee)\b", re.I),
     "pipe to interpreter / tee"),
    (re.compile(r"\bxargs\b[^|]*?\b(?:bash|sh|zsh|python|perl|node|eval)\b", re.I),
     "xargs to interpreter"),
    (re.compile(r"\b(?:curl|wget|fetch)\b[^|;&]*?\|\s*(?:bash|sh|zsh|python|perl)\b", re.I),
     "curl|bash pattern"),
    (re.compile(r"\beval\b", re.I), "eval"),
    (re.compile(r"`[^`]*`"), "backtick substitution"),
    (re.compile(
        r"\$\([^)]*(?:curl|wget|bash|sh|eval|rm|dd|mkfs)\b[^)]*\)", re.I),
     "destructive command substitution"),
    (re.compile(
        r"\bsystemctl\b\s+(?:start|stop|restart|enable|disable|mask|unmask|"
        r"kill|reset-failed|daemon-reload|reload)\b", re.I),
     "systemctl state change"),
    (re.compile(
        r"\bnmcli\b\s+(?:c|conn|connection)\s+(?:delete|modify|edit|add)\b", re.I),
     "nmcli destructive"),
    (re.compile(r"\biptables\b|\bnft\b", re.I), "firewall rule change"),
    (re.compile(r"\b(?:reboot|shutdown|halt|poweroff)\b", re.I), "power action"),
    (re.compile(r"\binit\s+[06]\b"), "init runlevel"),
    (re.compile(r"\bnc\b\s+-[a-z]*l", re.I), "netcat listen"),
    (re.compile(r"\bsocat\b", re.I), "socat"),
    (re.compile(r"\bpython\d*\b\s+-m\s+http\.server", re.I), "http.server"),
    (re.compile(r"\b(?:apt|apt-get|dpkg|aptitude)\b\s+"
                r"(?:install|remove|purge|upgrade|update|autoremove)\b", re.I),
     "package management"),
    (re.compile(r"\bpip\d*\b\s+install\b", re.I), "pip install"),
    (re.compile(r">>?\s*/(?:etc|opt|usr|boot|sbin|bin|lib|root)\b"),
     "redirect to system path"),
    (re.compile(
        r"\btee\b\s+(?:-[a-zA-Z]+\s+)*/(?:etc|opt|usr|boot|sbin|bin|lib|root)\b",
        re.I),
     "tee to system path"),
    (re.compile(r">>?\s*/dev/(?!null|stdout|stderr|tty\b)"),
     "redirect to device"),
    (re.compile(r"\bpkill\b|\bkillall\b", re.I), "kill by name"),
    (re.compile(r"\bcrontab\b\s+-[er]", re.I), "crontab edit"),
]


def is_destructive(command: str) -> Optional[str]:
    if not command:
        return None
    for pattern, reason in _DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return reason
    return None


# ── Dataclass ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Diagnostic:
    id: int
    label: str
    command: str
    description: Optional[str]
    sort_order: int


# ── On-disk cache ────────────────────────────────────────────────────────────


def load_cached_diagnostics() -> List[Diagnostic]:
    try:
        raw = DIAGNOSTICS_CACHE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: List[Diagnostic] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(
                Diagnostic(
                    id=int(entry["id"]),
                    label=str(entry.get("label") or ""),
                    command=str(entry.get("command") or ""),
                    description=entry.get("description"),
                    sort_order=int(entry.get("sort_order") or 0),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    out.sort(key=lambda d: (d.sort_order, d.id))
    return out


def get_diagnostic_by_id(diag_id: int) -> Optional[Diagnostic]:
    for d in load_cached_diagnostics():
        if d.id == int(diag_id):
            return d
    return None


def save_cached_diagnostics(diagnostics: List[Dict[str, Any]]) -> None:
    try:
        DIAGNOSTICS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DIAGNOSTICS_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(diagnostics, separators=(",", ":")), encoding="utf-8"
        )
        tmp.replace(DIAGNOSTICS_CACHE_PATH)
    except OSError as exc:
        logger.warning("could not write diagnostics cache: %s", exc)


def refresh_from_server(
    backend: str, token: str, *, timeout: float = 10.0
) -> Optional[List[Dict[str, Any]]]:
    if not backend or not token:
        return None
    url = backend.rstrip("/") + "/api/public/kiosk-os/diagnostics"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning("diagnostics: network error: %s", exc)
        return None
    if resp.status_code >= 400:
        logger.warning(
            "diagnostics: backend rejected status=%s", resp.status_code
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    diagnostics = data.get("diagnostics") or []
    if not isinstance(diagnostics, list):
        return None
    save_cached_diagnostics(diagnostics)
    return diagnostics
