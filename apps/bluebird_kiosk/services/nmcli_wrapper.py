"""Thin wrappers around `nmcli` for WiFi scan / connect / status.

We shell out rather than depend on a NetworkManager Python binding to keep
the package list short. Every call uses an explicit argv list (no shell=True)
to avoid command injection from user-controlled SSIDs and passwords.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional


NMCLI = "/usr/bin/nmcli"


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    signal: int          # 0–100
    security: str        # e.g., "WPA2", "WPA3", "" for open
    in_use: bool


def list_networks() -> List[WifiNetwork]:
    """Return visible WiFi networks, strongest signal first."""
    try:
        result = subprocess.run(
            [
                NMCLI,
                "--terse",
                "--escape", "no",
                "--fields", "IN-USE,SSID,SIGNAL,SECURITY",
                "device", "wifi", "list",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    nets: List[WifiNetwork] = []
    seen: set = set()
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        in_use = parts[0].strip() == "*"
        ssid = parts[1].strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        try:
            signal = int(parts[2].strip() or "0")
        except ValueError:
            signal = 0
        security = parts[3].strip()
        nets.append(
            WifiNetwork(ssid=ssid, signal=signal, security=security, in_use=in_use)
        )
    nets.sort(key=lambda n: (-n.signal, n.ssid.lower()))
    return nets


def connect(ssid: str, password: Optional[str] = None) -> tuple[bool, str]:
    """Connect to a WiFi network. Returns (ok, message)."""
    if not ssid:
        return False, "SSID is required."
    argv = [NMCLI, "device", "wifi", "connect", ssid]
    if password:
        argv += ["password", password]
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"nmcli failed: {exc}"
    if result.returncode == 0:
        return True, result.stdout.strip() or "Connected."
    return False, (result.stderr.strip() or result.stdout.strip() or "Unknown error.")


def current_status() -> dict:
    """Return a summary of the current connection — for display in admin UI.

    Uses two separate `nmcli` calls:
      - `nmcli device status` lists every interface + state + connection name
        (one device per line: device:type:state:connection)
      - `nmcli -g IP4.ADDRESS device show <dev>` returns the first IPv4 on
        the connected device (the field-name format only works with `show`)
    """
    eth = wifi = ip = None
    try:
        result = subprocess.run(
            [
                NMCLI,
                "--terse",
                "--fields", "DEVICE,TYPE,STATE,CONNECTION",
                "device", "status",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"ethernet": None, "wifi": None, "ip": None}

    primary_device = None
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        device, dev_type, state = parts[0], parts[1], parts[2]
        connection = parts[3] if len(parts) > 3 else ""
        # Treat "connected" as connected; "connected (externally)" (loopback /
        # ifupdown-managed) doesn't count. "disconnected" / "unmanaged" /
        # "connecting" likewise don't.
        if state != "connected":
            continue
        if dev_type == "ethernet" and not eth:
            eth = connection or device
            primary_device = device
        elif dev_type == "wifi" and not wifi:
            wifi = connection or device
            if not primary_device:
                primary_device = device

    if primary_device:
        try:
            ip_result = subprocess.run(
                [NMCLI, "-g", "IP4.ADDRESS", "device", "show", primary_device],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            first_line = ip_result.stdout.strip().splitlines()[0] if ip_result.stdout.strip() else ""
            if first_line:
                ip = first_line.split("/")[0]
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, IndexError):
            pass

    return {"ethernet": eth, "wifi": wifi, "ip": ip}
