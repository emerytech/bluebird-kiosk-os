# BlueBird Kiosk OS — Autoinstall ISO

A custom Ubuntu 24.04 Server ISO that boots **straight into a fully automated
install** of the BlueBird Kiosk OS. The operator's only interaction is selecting
a WiFi network and entering the password (skipped entirely if Ethernet is
plugged in).

## What it does

Flash the ISO to a USB stick, boot a fresh mini-PC from it, and:

1. GRUB defaults to `BlueBird Kiosk OS — autoinstall` after a 2-second timeout.
2. Subiquity starts in autoinstall mode — every screen is pre-answered from
   [`autoinstall/user-data`](autoinstall/user-data) **except** the network
   step (intentional: lets the operator pick a WiFi network if there's no
   Ethernet; one Done-click on Ethernet kiosks).
3. Wipes the first internal disk and installs Ubuntu Server 24.04.
4. At the end of the install, runs `curl https://bluebirdalerts.com/api/public/install-kiosk.sh | bash`
   inside the curtin chroot — that bootstrap apt-installs sway, chromium, greetd,
   Plymouth, etc., drops the kiosk launcher scripts and systemd units, activates
   the BlueBird boot splash, and configures GRUB.
5. Reboots. The kiosk lands directly in the BlueBird firstboot wizard
   (school slug + 6-digit admin PIN).

Total wall-clock from USB-boot to wizard: **~15 minutes** on a mid-range
mini-PC over a fast network.

## Default credentials

Baked into the ISO image. Rotate after first login if you want to harden.

| Field | Value |
|---|---|
| Username | `bluebird` |
| Password | `bluebird2026!!` |
| Hostname | `bluebird-kiosk` (the firstboot wizard can rename) |
| SSH | enabled (the kiosk-install default; turn off via `KIOSK_INSTALL_LOCKDOWN=1` if needed) |

The password is stored as a SHA-512 crypt hash in `autoinstall/user-data` —
to change it, regenerate the hash and re-commit:

```bash
openssl passwd -6 -salt 'BlueBirdKi' 'YOUR_NEW_PASSWORD'
```

## Building locally

```bash
# Ubuntu/Debian host
sudo apt install -y xorriso wget

cd iso-builder/
./build-iso.sh
# → dist/bluebird-kiosk-ubuntu-24.04.1-<git-short-sha>.iso
```

The first build downloads the ~3 GB stock Ubuntu Server ISO and caches it at
`~/.cache/bluebird-kiosk-os/`. Subsequent builds reuse the cache.

Environment overrides:

| Variable | Default | Purpose |
|---|---|---|
| `UBUNTU_VERSION` | `24.04.1` | Pin a different point release |
| `UBUNTU_ARCH` | `amd64` | (only amd64 is tested) |
| `SKIP_DOWNLOAD` | `0` | Reuse cached source ISO without checksum verification |
| `BB_KIOSK_CACHE` | `~/.cache/bluebird-kiosk-os` | Where to cache the source ISO |

## Building via CI

The [`build-autoinstall-iso.yml`](../.github/workflows/build-autoinstall-iso.yml)
GitHub Actions workflow builds the ISO on **release-tag push** (any tag matching
`v*` or `iso-*`) and attaches the resulting `.iso` + `.iso.sha256` to the
GitHub release as downloadable assets. Manually triggerable too via the
"Run workflow" button in the Actions tab.

## Flashing

The output ISO is a hybrid image that boots from both UEFI and BIOS systems.

```bash
# macOS / Linux
sudo dd if=dist/bluebird-kiosk-ubuntu-24.04.1-<sha>.iso \
        of=/dev/sdX bs=4M status=progress oflag=sync

# GUI tools that also work:
#   - balenaEtcher (cross-platform)
#   - Rufus (Windows; pick "DD Image" mode, not ISO mode)
#   - Ventoy (drop the ISO into the Ventoy USB)
```

Replace `/dev/sdX` with your USB device — **double-check with `lsblk` or
`diskutil list`** so you don't write over your laptop's disk.

## What you'll see at the kiosk

1. **Boot menu** — 2 seconds of GRUB. Hit Enter to skip the timeout.
2. **Subiquity loads** — black screen with a "configuring..." message for
   ~30 seconds while it picks the disk and confirms storage layout.
3. **Network step** — the one place the operator interacts:
   - Ethernet plugged in → screen shows DHCP-on-eth0 already up. Just hit
     **Done** to continue.
   - No Ethernet → pick a WiFi network from the list, enter the password,
     hit **Done**.
4. **Install runs unattended** for ~10–12 minutes (apt download is the long
   part — the late-command bootstrap takes ~3 minutes after Subiquity finishes
   the base install).
5. **First reboot** — the kiosk Plymouth splash appears (Legacy Wall badge
   on dark slate). About 5–10 seconds.
6. **BlueBird firstboot wizard** — chromium fullscreen, walks through:
   - WiFi (re-shown only if no Ethernet — same UX as the kiosk's own wizard)
   - School slug
   - 6-digit admin PIN
7. **Second reboot** — into the Legacy Wall.

## Notes

- **Network requirements during install:** the late-command needs to reach
  `bluebirdalerts.com` to download `install-kiosk.sh` and the kiosk-os tarball
  (~76 KB total) + the apt mirrors. If your school network filters HTTPS
  egress, allowlist `bluebirdalerts.com` and the Ubuntu mirrors before
  flashing.
- **`bluebird` user has sudo by default** (created via Subiquity's `identity`
  block, which adds the user to the `sudo` group automatically). Keep that
  in mind if you're hardening — `passwd -l bluebird` after install would
  lock yourself out unless `KIOSK_INSTALL_LOCKDOWN=1 KIOSK_INSTALL_KEEP_SSH=1`
  was set so you can still SSH in.
- **Re-running the kiosk install** is safe and idempotent — the bootstrap
  `install-kiosk.sh` is designed to be re-run for updates.
