# Installing BlueBird Kiosk OS

This guide is for school IT staff who want to turn a mini-PC into a BlueBird
Legacy Wall kiosk.

## Prerequisites

- A reasonably modern **x86_64 mini-PC** (Beelink, Intel NUC, GMKtec, or
  similar). At least 4 GB RAM and 64 GB internal storage.
- A **USB stick** (8 GB or larger) for the installer.
- A **monitor** and a **USB keyboard** for the install. Once installed, no
  keyboard is needed.
- The **school slug** assigned by BlueBird (e.g. `nen`).

## Step 1 — Create the installer USB

1. Download `bluebird-kiosk-os.iso` from
   `https://bluebird-alerts.com/downloads/kiosk-os/latest`.
2. Verify the SHA-256 checksum listed on that page matches the file you
   downloaded.
3. Flash the ISO to a USB stick with **Balena Etcher**
   (`https://etcher.balena.io`) or the equivalent tool on Windows / macOS / Linux.

## Step 2 — Install onto the kiosk

1. Plug the USB stick and a keyboard into the mini-PC.
2. Power on. Press the BIOS boot-menu key (usually `F7`, `F10`, `F11`, or
   `Esc`) and choose the USB device.
3. The installer (Calamares) opens. Click **Install BlueBird Kiosk OS**.
4. Choose the target disk. **The disk will be wiped.** Do not run this on a
   device with files you want to keep.
5. Click **Install** and wait 5–10 minutes.
6. When prompted, remove the USB stick and click **Reboot**.

## Step 3 — First-boot wizard

After the reboot the kiosk shows a setup wizard.

1. **Network.** Pick a WiFi network and enter the password. Skip this step if
   you've plugged in an Ethernet cable.
2. **School slug.** Type the slug you were given. The wizard will check the
   slug exists and has Legacy Wall enabled before continuing.
3. **Admin PIN.** Choose a 6-digit PIN. **Write this down somewhere safe** —
   without it, you can't fix WiFi or display issues on this kiosk.
4. The kiosk will reboot one more time and open Legacy Wall in fullscreen.

## Step 4 — Verify

- Legacy Wall fills the entire screen with no browser UI.
- Five-finger long-press anywhere should open the admin overlay. Enter the
  PIN to confirm it works.
- Open the BlueBird super-admin **Devices** view; this kiosk should appear
  as online within 1 minute.

## Troubleshooting

| Symptom | Try |
|---|---|
| Installer doesn't boot | Press BIOS boot-menu key. Disable Secure Boot if available. |
| WiFi doesn't appear | Plug in Ethernet for first-boot; configure WiFi later via admin overlay. |
| "Unknown school slug" | Confirm the slug with BlueBird support. Spelling matters; no spaces or capitals. |
| Forgot admin PIN | Reboot from the installer USB and run the installer again. |

Full admin reference: [ADMIN.md](ADMIN.md).
