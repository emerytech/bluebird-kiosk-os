# BlueBird Kiosk OS — On-site Admin Guide

A short reference for on-site staff (school IT, building admins, or facilities)
who need to fix something on a kiosk.

## Opening the admin overlay

**Five-finger long-press anywhere on the screen for 2 seconds.** A small admin
window opens on top of Legacy Wall.

If five fingers don't work, plug in a USB keyboard and press **Ctrl + Alt + B**
as a fallback.

Enter your 6-digit PIN. Three wrong attempts will lock the overlay for 5 minutes.

## Tabs

### Network

- Shows your current Ethernet / WiFi connection and IP address.
- Lists all visible WiFi networks. Tap one to connect; you'll be prompted for
  the password.
- Use this when the school WiFi password has changed and the kiosk can't
  reconnect.

### Display

- **Output** — which monitor or HDMI output to configure (most kiosks have one).
- **Rotation** — set to 90°/180°/270° if the screen is sideways or upside down.
- **Brightness** — for displays that support it.

Changes apply instantly.

### Kiosk

- Shows the school slug and Legacy Wall URL the kiosk is currently pointed at.
- **Restart kiosk** — kills and respawns Chromium (use if the gallery is frozen).
- **Change school slug** — only use this if instructed by BlueBird support.

### System

- **Reboot** / **Shut down** — full power cycle.
- **Change PIN** — change the 6-digit admin PIN.
- **View recent logs** — the last 200 lines of kiosk activity for support.
- **Factory reset** — wipes the school slug, PIN, and device ID. The next boot
  will return to the first-boot setup wizard. Only use this if you're moving
  the kiosk to a different school.

## When to call BlueBird support

- The kiosk shows a blank or black screen and the admin overlay doesn't open.
- After a reboot the kiosk no longer connects to the BlueBird backend.
- Legacy Wall loads but shows the wrong school's photos.
- Anything that looks like data from another school.
