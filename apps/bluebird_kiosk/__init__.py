"""BlueBird Kiosk OS on-device control plane.

This is a small FastAPI application that runs on every BlueBird Kiosk OS
device. It is bound to 127.0.0.1 and serves two surfaces:

    /firstboot/...  — the on-screen setup wizard (WiFi → school slug → PIN)
    /admin/...      — the PIN-locked device-admin overlay (network, display,
                       kiosk control, system, support)

A separate Chromium kiosk window points at one of those paths depending on
whether the device has been configured yet.
"""

__version__ = "0.1.0"
