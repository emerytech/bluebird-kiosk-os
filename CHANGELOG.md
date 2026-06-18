# BlueBird Kiosk OS — Change Log

Most recent first. These notes are shown in the admin overlay before you apply a
kiosk update, so an operator can see what an update will install before confirming.

## 2026-06-18
- **Signage / power-schedule fix:** the heartbeat service could not save settings
  pushed from the console — assigning a kiosk to a Beacon display did nothing, and
  the power on/off schedule was never cached locally. The service was sandboxed
  away from its own config files; it can now write them again. (No action needed;
  applies on update.)

## 2026-06-17
- **Touch WiFi fix:** connecting to WiFi from the admin overlay now shows the
  on-screen keyboard when you tap the password field — both for a network you
  pick from the list and for "Join hidden network." Previously no keyboard
  appeared, so a touch-only kiosk couldn't join a password-protected network.
- **Touch admin fix:** "Change school slug" and "Change PIN" now use an on-screen
  keyboard too (they previously relied on a popup that a touch-only kiosk blocks).
- **Digital signage (Beacon):** a kiosk can now be assigned a BlueBird Beacon
  signage display from the admin console. When assigned it switches to showing
  that display; the full-screen emergency takeover still appears during an alert,
  exactly as on the Legacy Wall. Unassigned kiosks are unaffected.
- **Video caching:** background-rotation and on-demand aerial videos now play from
  the on-device cache instead of re-streaming from the internet on every cycle —
  cutting wall bandwidth sharply and keeping videos playing through brief network
  outages (the photo cache already worked this way).

## 2026-06-16
- **Update preview:** pressing **Update kiosk** now shows these change notes and
  asks you to confirm before anything is installed.
- **Self-recovery:** the kiosk detects a wedged display — e.g. stuck on a
  Cloudflare error screen after a brief server outage — and restarts the session
  on its own, so it no longer needs a manual power-cycle.
- **Power saver:** optional scheduled display off/on (DPMS) to conserve power
  outside school hours.

## Earlier
- Seamless boot hand-off with a branded loading wallpaper (no flicker to a
  desktop during startup).
- On-wall emergency incident takeover: when the school triggers an alarm, every
  kiosk shows a full-screen incident notice, then clears automatically.
- Local image cache + offline slideshow fallback so the wall keeps showing photos
  even if the network drops.
