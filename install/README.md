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
- Debian 12 "bookworm", Debian 13 "trixie", or Ubuntu 24.04 LTS Server,
  freshly installed, command-line only (no desktop environment).
- An admin user with sudo.

**Ubuntu vs Debian:** the installer auto-detects which one you're on and:
- Substitutes Ubuntu's `chromium-browser` (a snap-wrapper apt package) for
  Debian's `chromium`, then symlinks `/usr/bin/chromium` to wherever the
  snap binary ended up so all the launcher scripts keep working.
- Substitutes Ubuntu's `linux-firmware` for Debian's split
  `firmware-iwlwifi` / `firmware-realtek` / `firmware-misc-nonfree`.
- Skips the Debian-only "enable non-free-firmware component" sources tweak.

Either distro works for the production kiosk role; Debian is what's been
tested most.

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

## Defaults

A vanilla install (no flags) is **open + manageable**:

- **SSH enabled** — `openssh-server` is installed unconditionally, so you can `ssh` / `scp` into the kiosk for remote management
- **TTY switching reachable** — `Ctrl+Alt+F2` reaches a console login
- **Root unlocked** — set its password by hand if you want
- **No VT/getty masking**

This is the right shape for in-house kiosks, test units, and anything you want to be able to poke at over SSH. Add `--lockdown` for the production-hardening pass when a kiosk goes into a hallway.

## Flags

| Flag | What it does |
|---|---|
| `--lockdown` | **Opt in to the full harden** — `NAutoVTs=0`, getty@tty[2-6] masked, root account locked (`passwd -l root`), and (unless `--keep-ssh` is also set) `ssh.service` disabled. Use for kiosks deployed in public/hallway spaces. Re-running the installer without `--lockdown` lifts the harden (TTYs unmasked, logind conf removed); root stays locked — set the root password by hand if you need it. |
| `--keep-ssh` | When combined with `--lockdown`, keep `ssh.service` enabled (full harden minus the ssh-disable step). No effect without `--lockdown` — SSH is on by default. |
| `--no-firstboot` | Don't auto-launch the firstboot wizard. Use if you've pre-baked `/etc/bluebird/kiosk.conf` with the slug + PIN. |
| `--debug` | Backward-compat no-op. The "open + manageable" behavior `--debug` used to gate is now the default. Accepted silently so older copies of the bootstrap don't break. |

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
