# BlueBird Kiosk OS — Change Log

Most recent first. These notes are shown in the admin overlay before you apply a
kiosk update, so an operator can see what an update will install before confirming.

## 2026-06-19
- **Display settings stick:** the rotation, resolution, and brightness you set on a kiosk now
  survive reboots and updates — a portrait hallway board stays portrait, and a re-plugged HDMI
  comes back with its saved rotation. (No action needed; applies on update.)
- **Multiple displays + HDMI hotplug:** plug a second (or third) screen into a kiosk and it now
  lights up showing the board within ~a second — no reboot. By default every screen **mirrors**
  the same board (the common hallway case); set `KIOSK_DISPLAY_MODE=extend` in the kiosk config
  to keep sway's side-by-side layout instead. Single-screen kiosks are unaffected. (No action
  needed; applies on update.)
- **Resilience + cleanup tune-up:** fixed a case where a kiosk that was *online but showing the
  offline board* (or on the firstboot setup screen) could mistakenly reboot-loop itself; capped
  on-device logs (now kept across reboots for remote diagnosis) and turned off coredumps so a
  kiosk can't slowly fill its own disk; halved screenshot frequency (lighter on the network);
  bounded the browser cache; and disabled browser DevTools (security). (No action needed; applies
  on update.)
- **Easier admin access on touch kiosks:** open the admin panel by **tapping any screen
  corner 5 times** (within a few seconds) — works on any touchscreen, including 2-touch
  panels. The old 5-finger hold still works too, and **both now reliably bring the panel to
  the front**: previously the gesture could open the admin window *behind* the fullscreen
  display, so it looked like nothing happened. (No action needed; applies on update.)
- **Tap to wake during quiet hours:** when a display is scheduled to sleep (power
  schedule), a single touch now wakes the screen for **30 minutes** before it returns
  to sleep — and each tap restarts the 30-minute timer. Great for a hallway board that's
  off after hours when someone walks up to check it. Emergency alerts still force the
  screen on regardless. (No action needed; applies on update.)
- **Forget a WiFi network:** the admin overlay's Network tab now has a **Saved
  networks** list — tap **Forget** (then tap again to confirm) to remove a network
  the kiosk remembers, so it stops auto-reconnecting to a wrong or old one. (No
  action needed; applies on update.)
- **Offline admin access:** if a kiosk boots with no network it shows the cached
  slideshow — now you can reach the admin panel to join WiFi by **tapping the
  top-left corner 5 times** (opens the PIN screen). Previously the only way in was
  an undiscoverable 5-finger hold, so a kiosk at a new location couldn't get
  online. (The 5-finger hold still works too.)
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
