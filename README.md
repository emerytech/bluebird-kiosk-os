# BlueBird Kiosk OS

A purpose-built Debian-based Linux image that boots straight into the BlueBird
Legacy Wall kiosk in fullscreen. Hardened against tampering, configured at
first boot through an on-screen wizard, and serviced through a hidden
PIN-locked admin overlay.

Target hardware: x86_64 mini-PC (Beelink / NUC / GMKtec class).

## Components

- **`build/`** — Reproducible ISO build via Debian `live-build` + `calamares`.
  Produces a bootable installer that wipes the target disk.
- **`apps/bluebird_kiosk/`** — On-device FastAPI app (loopback only) that
  powers both the first-boot wizard and the device-admin overlay.
- **`apps/bluebird_gesture/`** — `libinput`-based daemon that watches for the
  5-finger long-press gesture and signals the admin overlay to open.
- **`docs/`** — Install guide for school IT, on-site admin guide for staff,
  and build guide for engineers.

## High-level flow

```
flash USB → boot installer → Calamares wipes disk → reboot
                                                      │
                                                      ▼
                                          bluebird-firstboot.service
                                          (WiFi → school slug → admin PIN)
                                                      │
                                                      ▼
                                          bluebird-kiosk.target
                                          (sway → chromium --kiosk → /{slug}/legacy-wall)
```

5-finger long-press anywhere on the touchscreen opens the device-admin overlay
(WiFi, display, system tools) gated by a 6-digit PIN.

## Backend touchpoints

The kiosk talks to the existing BlueBird backend over two **public** endpoints
that bypass tenant middleware and resolve `tenant_id` from the slug
server-side:

- `GET  /api/public/legacy-wall/exists?slug=...` — slug validation for the
  first-boot wizard.
- `POST /api/public/kiosk/heartbeat` — once-per-minute liveness ping with
  device + OS version metadata.

See `backend/app/api/public_kiosk_routes.py` and
`backend/app/services/kiosk_device_store.py`.

## Status

This is the v1 scaffold. The kiosk-app Python package and gesture daemon ship
runnable on a developer's laptop today; the live-build ISO build has been
designed and templated but not yet baked end-to-end on real hardware. See
`docs/BUILD.md` for the current build instructions.
