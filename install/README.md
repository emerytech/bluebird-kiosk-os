# BlueBird Kiosk Installer

Convert a fresh Debian 12 / Debian 13 / Ubuntu 24.04 install into a BlueBird
Legacy Wall kiosk. This is the **recommended v1 deployment path** — simpler, more
reliable, and faster to iterate than the custom ISO build under
`kiosk-os/build/`.

The custom ISO line of work is parked until install-script-based deployment
proves out at scale.

## Prerequisites

- An x86_64 PC (mini-PC, NUC, repurposed laptop — anything Debian boots on).
- A working internet connection (script downloads ~700 MB of apt packages
  the first time).
- Debian 12 "bookworm", Debian 13 "trixie", or Ubuntu 24.04 LTS, freshly
  installed, command-line only (no desktop environment).
- An admin user with sudo.

## Install — quick path

On the target machine, with internet access:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/emerytech/bluebird-emergency-alerts /tmp/bb
sudo bash /tmp/bb/kiosk-os/install/install.sh
```

Takes 5–15 minutes depending on network speed. After it finishes:

```bash
sudo reboot
```

The kiosk reboots into the first-boot wizard. Walk through WiFi → school
slug → 6-digit admin PIN. Wizard reboots again and the kiosk lands at
Legacy Wall fullscreen.

## Install — offline / private path

If the repo is private, or the target has no internet, do the apt install
yourself, then `scp` the `kiosk-os/` directory across:

```bash
# On your workstation:
scp -r kiosk-os/ user@target:/tmp/

# On the target:
sudo bash /tmp/kiosk-os/install/install.sh
```

## Flags

| Flag | What it does |
|---|---|
| `--debug` | Skip the lockdown step. VT switching (`Ctrl+Alt+F2`) stays available, extra TTYs unmasked, root unlocked, ssh enabled. Use during development. Re-run without `--debug` to apply the harden later. |
| `--keep-ssh` | Apply the full lockdown (TTYs masked, root locked) **but leave the SSH server installed + enabled**, so you can `ssh`/`scp` into the kiosk for remote management. Installs `openssh-server` if missing. Recommended for kiosks you'll manage remotely. |
| `--no-firstboot` | Don't auto-launch the firstboot wizard. Use if you've pre-baked `/etc/bluebird/kiosk.conf` with the slug + PIN. |

All flags are idempotent — safe to combine, safe to re-run.

**Field terminal:** on any installed kiosk, **`Ctrl+Alt+T`** opens a PIN-gated
terminal (enter the 6-digit admin PIN). Works even on a fully hardened kiosk —
the deliberate shell escape hatch for on-site troubleshooting without an SSH
session or keyboard TTY.

## What it does

In order:

1. Sanity-checks Debian 12 / Ubuntu 24.04 + root.
2. Enables `non-free-firmware` apt component if missing.
3. `apt install`s sway, chromium, network-manager, greetd, python3 + deps,
   firmware packages, etc. (~700 MB).
4. Creates the `bluebird-kiosk` system user (UID/GID 1000).
5. Copies and `pip install`s the `bluebird_kiosk` + `bluebird_gesture`
   Python packages from this repo.
6. Drops systemd units, sway config, polkit rules, kiosk launcher scripts.
7. Configures greetd for autologin into sway.
8. Configures unattended-upgrades (security pocket only, 03:00 reboot
   window).
9. Enables all kiosk services + sets `bluebird-kiosk.target` as the systemd
   default.
10. **Unless `--debug`**: masks extra getty TTYs, disables ssh, locks root,
    applies logind hardening.

## Updating an existing kiosk

To update an already-deployed kiosk:

```bash
# As an admin user on the kiosk (use --debug if SSH/login is locked down):
cd /tmp/bb && git pull
sudo bash /tmp/bb/kiosk-os/install/install.sh
sudo reboot
```

The script is idempotent — re-running it just refreshes file content, no
data loss.

## Accessing the device-admin overlay

After install, the kiosk runs Chromium fullscreen on Legacy Wall. There are
two ways to open the admin overlay (a separate PIN-gated Chromium window
with WiFi / display / kiosk / system tools, including **Factory reset**):

| Input method | Gesture |
|---|---|
| Touchscreen | **5-finger long-press** anywhere for ~2 s |
| Keyboard | **`Ctrl + Alt + B`** |

Either opens an 800×1000 Chromium window pointed at the local admin app.
Enter your 6-digit PIN, then use the tabs at top (Network / Display /
Kiosk / **System**).

The **Factory reset** button lives under the System tab. It deletes:
- `/etc/bluebird/configured`
- `/etc/bluebird/admin.pin`
- `SCHOOL_SLUG` / `LEGACY_WALL_URL` / `DEVICE_ID` from `kiosk.conf`

…then reboots into the firstboot wizard. Useful when redeploying a kiosk
to a different school.

## Recovery (if you've lost access)

- **Lost the admin PIN** — `sudo rm /etc/bluebird/admin.pin && sudo reboot`, then the firstboot wizard re-runs.
- **Forgot the school slug / want to change it** — `sudo rm /etc/bluebird/configured && sudo reboot`.
- **Kiosk won't reach Legacy Wall** — boot, switch to TTY (only works if `--debug` was used or you've lifted the lockdown), check `journalctl -u bluebird-kiosk -u bluebird-admin`.
- **Need to SSH back in to a locked-down kiosk** — re-image the disk, or boot a rescue USB and remove `/etc/systemd/logind.conf.d/bluebird-kiosk.conf` + unmask `getty@tty2.service`.

## Tenant isolation

The install script is concerned only with hardware setup — tenant
isolation lives entirely in the backend (`tenant_manager.school_for_slug`
in `backend/app/services/tenant_manager.py`). The kiosk identifies itself
by school slug + device ID, never by `tenant_id` directly.

See `docs/memory/ARCHITECTURE.md` "BlueBird Kiosk OS" section for the full
isolation model.
