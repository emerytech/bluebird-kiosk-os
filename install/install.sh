#!/usr/bin/env bash
# BlueBird Kiosk installer — turn a fresh Debian 12 / Ubuntu 24.04 system into a
# BlueBird Legacy Wall kiosk. Run on the target machine after git-cloning the
# bluebird-emergency-alerts repo (or with KIOSK_REPO_ROOT pointing at a copy).
#
#   sudo apt install -y git
#   git clone https://github.com/emerytech/bluebird-emergency-alerts /tmp/bb
#   sudo bash /tmp/bb/kiosk-os/install/install.sh
#
# Flags:
#   --lockdown       OPT IN to the full harden: VT switching disabled, extra
#                    TTYs masked, root locked, ssh disabled. Use for kiosks
#                    deployed in public/hallway spaces where you don't want
#                    a student finding a shell. Re-run without --lockdown to
#                    lift the harden later (root stays locked — set the root
#                    password by hand if you need it).
#   --keep-ssh       When combined with --lockdown, keep the SSH server
#                    enabled (full harden minus the ssh-disable step). No
#                    effect without --lockdown — ssh is on by default.
#   --no-firstboot   Don't auto-launch the firstboot wizard on next reboot.
#                    Use if you want to drop the device into kiosk mode via
#                    a pre-baked /etc/bluebird/kiosk.conf.
#   --debug          Backward-compat no-op. The "open + manageable" behavior
#                    this used to enable is now the default; the flag is
#                    accepted so older copies of the bootstrap script don't
#                    break.
#
# Default behavior (no flags): SSH on, TTY switching reachable (Ctrl+Alt+F2),
# root unlocked, all sshd defaults. Optimized for development + operator
# access. Add --lockdown for the production-hardening pass.
#
# Idempotent: safe to re-run. Each step checks state before acting.

set -euo pipefail

# ── Locate the repo root (this script is at kiosk-os/install/install.sh) ─────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${KIOSK_REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
KIOSK_OS="$REPO_ROOT/kiosk-os"
LIVE_BUILD_INC="$KIOSK_OS/build/live-build/config/includes.chroot"

# ── Flags ─────────────────────────────────────────────────────────────────────
LOCKDOWN=0
SKIP_FIRSTBOOT=0
KEEP_SSH=0
for arg in "$@"; do
  case "$arg" in
    --lockdown) LOCKDOWN=1 ;;
    --keep-ssh) KEEP_SSH=1 ;;
    --no-firstboot) SKIP_FIRSTBOOT=1 ;;
    --debug)
      # Backward-compat no-op — the open behavior --debug used to gate is
      # now the default. Accept the flag silently so older copies of the
      # /api/public/install-kiosk.sh bootstrap don't break.
      :
      ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2; exit 2
      ;;
  esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
[[ "$(id -u)" -eq 0 ]] || die "must run as root (sudo bash $0 ...)"

[[ -d "$KIOSK_OS" ]] || die "can't find kiosk-os/ at $KIOSK_OS — set KIOSK_REPO_ROOT or run from the repo"
[[ -d "$KIOSK_OS/apps/bluebird_kiosk" ]] || die "missing apps/bluebird_kiosk — repo layout wrong?"
[[ -d "$LIVE_BUILD_INC" ]] || die "missing $LIVE_BUILD_INC — repo layout wrong?"

# Distro check.
if ! grep -qE 'ID=(debian|ubuntu)' /etc/os-release; then
  die "this script only supports Debian 12+ / Ubuntu 24.04+ — refusing to run on $(grep ^PRETTY_NAME /etc/os-release)"
fi
. /etc/os-release
case "${VERSION_ID:-}" in
  12|13|24.04) ;;
  *) warn "running on ${ID} ${VERSION_ID:-unknown} (tested on Debian 12 / 13 + Ubuntu 24.04) — proceeding anyway" ;;
esac

# Distro family — Ubuntu and Debian diverge in a few package names + the
# Chromium delivery mechanism (Ubuntu ships a snap; Debian ships an apt
# package). Branch on this throughout the rest of the script.
IS_UBUNTU=0
[[ "${ID:-}" == "ubuntu" ]] && IS_UBUNTU=1

log "kiosk install starting (repo: $REPO_ROOT, distro=${ID:-?}/${VERSION_ID:-?}, lockdown=$LOCKDOWN, keep-ssh=$KEEP_SSH, firstboot=$((1 - SKIP_FIRSTBOOT)))"

# ── apt packages ─────────────────────────────────────────────────────────────
log "installing apt packages (this is the slow step)"

# Ensure non-free-firmware is enabled (Intel/Realtek firmware lives there).
# Debian 12 uses /etc/apt/sources.list (one-line format). Debian 13 defaults to
# /etc/apt/sources.list.d/debian.sources (deb822 format). Handle both.
# On Ubuntu this is a no-op — Ubuntu bundles the same firmware into the
# linux-firmware package which lives in main and needs no extra component.
enable_non_free_firmware() {
  if [[ "$IS_UBUNTU" -eq 1 ]]; then
    return 0
  fi
  local changed=0
  if [[ -f /etc/apt/sources.list ]] && grep -q '^deb ' /etc/apt/sources.list; then
    if ! grep -q 'non-free-firmware' /etc/apt/sources.list; then
      log "  enabling non-free + non-free-firmware in /etc/apt/sources.list"
      sed -i.bak -E 's|^(deb .*\<main\>.*)$|\1 contrib non-free non-free-firmware|' /etc/apt/sources.list
      changed=1
    fi
  fi
  if [[ -f /etc/apt/sources.list.d/debian.sources ]]; then
    if ! grep -q 'non-free-firmware' /etc/apt/sources.list.d/debian.sources; then
      log "  enabling non-free + non-free-firmware in /etc/apt/sources.list.d/debian.sources"
      sed -i.bak -E 's|^(Components:.*)$|\1 contrib non-free non-free-firmware|' /etc/apt/sources.list.d/debian.sources
      changed=1
    fi
  fi
  if [[ $changed -eq 1 ]]; then
    apt-get update -qq
  fi
}

DEBIAN_FRONTEND=noninteractive apt-get update -qq
enable_non_free_firmware

# Distro-specific package names.
#
# - Chromium: Debian ships a real apt package (`chromium`). Ubuntu's `chromium`
#   apt package is a transitional snap (the binary lives at /snap/bin/chromium,
#   not /usr/bin/chromium). We install the Ubuntu transitional package and
#   then symlink /usr/bin/chromium after the apt step.
# - Firmware: Debian splits NIC firmware across `firmware-iwlwifi`,
#   `firmware-realtek`, and `firmware-misc-nonfree`. Ubuntu bundles all of
#   that into the single `linux-firmware` package (and Ubuntu's
#   `firmware-linux` doesn't exist).
if [[ "$IS_UBUNTU" -eq 1 ]]; then
  CHROMIUM_PKG=chromium-browser
  FIRMWARE_PKGS=(linux-firmware)
else
  CHROMIUM_PKG=chromium
  FIRMWARE_PKGS=(firmware-linux firmware-iwlwifi firmware-realtek firmware-misc-nonfree)
fi

# Mirror of kiosk-os/build/live-build/config/package-lists/bluebird-kiosk.list.chroot,
# minus things only relevant inside a live ISO build (debian-installer, etc.)
APT_PACKAGES=(
  # Display stack
  sway swayidle swaylock seatd xwayland
  mesa-vulkan-drivers libgl1-mesa-dri

  # Greeter
  greetd

  # Browser (distro-branched: see CHROMIUM_PKG above)
  "$CHROMIUM_PKG"

  # Terminal emulator — only reachable via the PIN-gated Ctrl+Alt+T
  # keybinding for field troubleshooting (see pin-terminal launcher).
  foot

  # Networking
  network-manager wpasupplicant iw dnsutils iputils-ping

  # Display utilities
  brightnessctl wlr-randr

  # Screenshot capture for the fleet console (super-admin sees a live
  # thumbnail of every kiosk). `grim` grabs the current sway output as
  # PNG; bluebird-screenshot.timer fires every 60s.
  grim

  # Input + gesture daemon dependencies
  libinput-tools python3-evdev

  # Python runtime for our app
  python3 python3-pip python3-venv
  python3-fastapi python3-uvicorn python3-jinja2 python3-pydantic
  python3-bcrypt python3-requests

  # Firmware for common WiFi/eth chips (distro-branched: see FIRMWARE_PKGS above)
  "${FIRMWARE_PKGS[@]}"

  # Authorization daemon — required for the kiosk user to call
  # `systemctl reboot/poweroff` and NetworkManager actions without a password.
  # On Debian 13 polkit is NOT pulled in as a dependency of NetworkManager
  # or systemd-logind with --no-install-recommends, so we have to ask for it
  # explicitly. Without it, the firstboot wizard's "Finish Setup → reboot"
  # silently fails with "Call to Reboot failed: Access denied".
  polkitd

  # Boot splash — Plymouth shows the Legacy Wall badge from initramfs
  # handoff through to greetd. plymouth-themes brings in the default
  # themes (we activate `bluebird` later); plymouth-x11 isn't needed
  # since we're Wayland-only.
  plymouth plymouth-themes

  # Fonts — minimal Ubuntu ships NO color-emoji font, so Chromium renders the
  # emoji in the Legacy Wall UI (🤝 Share, 🎥 View Aerial, 📆 Filter Year,
  # 🔍 search, etc.) as blank/tofu while plain symbols (▶ ⏸) still work. Noto
  # Color Emoji is the standard fix; fontconfig auto-registers it as the emoji
  # fallback so it works in Chromium with no extra config.
  fonts-noto-color-emoji

  # Maintenance
  sudo systemd systemd-timesyncd unattended-upgrades ca-certificates

  # Used by the install script itself
  git rsync
)

DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"

# Ubuntu-specific Chromium handling. The default `chromium-browser` apt
# package is a transitional snap, and snap Chromium can't be a kiosk
# browser (sandbox blocks --user-data-dir outside snap-confined paths,
# Wayland interface needs explicit connection). Swap to a real apt-managed
# build from the xtradeb PPA — community-maintained, ships /usr/bin/chromium
# as a regular binary wrapper.
if [[ "$IS_UBUNTU" -eq 1 ]]; then
  log "Ubuntu: ensuring real apt chromium from xtradeb PPA (not the snap)"
  # Yank the snap variant if a previous installer or apt brought it in.
  # Only rm /usr/bin/chromium if it's a symlink (the snap-shim chain we
  # used to install). A real apt-managed chromium leaves a regular file
  # there and we must NOT delete it — apt won't repopulate it on
  # re-install without --reinstall.
  if [[ -L /usr/bin/chromium ]]; then
    log "  removing stale /usr/bin/chromium symlink → $(readlink /usr/bin/chromium)"
    rm -f /usr/bin/chromium
  fi
  rm -f /snap/bin/chromium 2>/dev/null || true
  snap remove chromium 2>/dev/null || true
  apt-get remove --purge -y chromium-browser 2>/dev/null || true
  # Add the PPA (idempotent — add-apt-repository -y is safe to re-run).
  apt-get install -y --no-install-recommends software-properties-common
  add-apt-repository -y ppa:xtradeb/apps
  apt-get update -qq
  # Install (or reinstall to repopulate files if a previous run deleted the
  # binary while leaving the package installed).
  if dpkg -s chromium >/dev/null 2>&1 && [[ ! -x /usr/bin/chromium ]]; then
    apt-get install --reinstall -y --no-install-recommends chromium
  else
    apt-get install -y --no-install-recommends chromium
  fi
  if [[ ! -x /usr/bin/chromium ]]; then
    die "Ubuntu: xtradeb chromium install left no /usr/bin/chromium — investigate before continuing"
  fi
  # Ubuntu 23.10+ restricts unprivileged user namespaces via AppArmor,
  # which Chromium's sandbox depends on. Without this sysctl, every
  # `/usr/bin/chromium` invocation under sway dies with "No usable
  # sandbox!" before painting anything. The setting is harmless on a
  # single-purpose kiosk (we run one trusted Chromium and nothing else).
  log "Ubuntu: enabling unprivileged user namespaces (chromium sandbox prereq)"
  sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 >/dev/null
  printf 'kernel.apparmor_restrict_unprivileged_userns=0\n' \
    > /etc/sysctl.d/60-bluebird-kiosk-userns.conf
fi

# ── User + directories ───────────────────────────────────────────────────────
log "creating bluebird-kiosk system group + user"
# System user (UID < 1000), auto-assigned. Decoupled from whatever UID the
# Debian installer gave your admin account — they don't collide.
if ! getent group bluebird-kiosk >/dev/null; then
  groupadd --system bluebird-kiosk
fi
if ! id bluebird-kiosk >/dev/null 2>&1; then
  useradd --system --gid bluebird-kiosk \
    --home-dir /var/lib/bluebird-kiosk --shell /usr/sbin/nologin \
    --no-create-home bluebird-kiosk
fi
KIOSK_UID="$(id -u bluebird-kiosk)"
log "  bluebird-kiosk UID=$KIOSK_UID"
install -d -o bluebird-kiosk -g bluebird-kiosk /var/lib/bluebird-kiosk
install -d -o bluebird-kiosk -g bluebird-kiosk /etc/bluebird
# systemd-logind manages /run/user/$KIOSK_UID automatically once greetd opens
# a session, so we don't pre-create it here.

# ── Python packages ──────────────────────────────────────────────────────────
log "staging bluebird_kiosk + bluebird_gesture Python packages"
# We rsync the package directories to /opt/bluebird-kiosk/src/ and rely on
# PYTHONPATH=/opt/bluebird-kiosk/src in the systemd units. No pip install
# needed; the apt-installed python3-fastapi / python3-uvicorn / etc. provide
# the third-party deps.
install -d /opt/bluebird-kiosk/src
rsync -a --delete "$KIOSK_OS/apps/bluebird_kiosk/" /opt/bluebird-kiosk/src/bluebird_kiosk/
rsync -a --delete "$KIOSK_OS/apps/bluebird_gesture/" /opt/bluebird-kiosk/src/bluebird_gesture/

# Chromium MV3 extension that redirects Legacy Wall media requests
# from bluebird-alerts.com to the local /var/lib/bluebird-kiosk/media
# cache via 127.0.0.1:7311. See apps/bluebird_kiosk_cache_ext/ for the
# manifest + rules. Loaded by /opt/bluebird-kiosk/bin/launch-kiosk-chromium
# via --load-extension.
log "staging chromium cache-extension"
install -d /opt/bluebird-kiosk/share/cache-ext
rsync -a --delete "$KIOSK_OS/apps/bluebird_kiosk_cache_ext/" /opt/bluebird-kiosk/share/cache-ext/
# Chromium writes its compiled DNR ruleset into
# <ext>/_metadata/generated_indexed_rulesets/ on first load. Without
# write perms, extension load fails with "Internal error while parsing
# rules" (field-verified 2026-06-04 via --enable-logging=stderr).
# Owner the whole dir to bluebird-kiosk so chromium's user can write.
chown -R bluebird-kiosk:bluebird-kiosk /opt/bluebird-kiosk/share/cache-ext/

# ── Drop /etc and /opt files ─────────────────────────────────────────────────
log "installing systemd units"
install -m 0644 "$LIVE_BUILD_INC"/etc/systemd/system/bluebird-*.service \
                "$LIVE_BUILD_INC"/etc/systemd/system/bluebird-*.timer \
                "$LIVE_BUILD_INC"/etc/systemd/system/bluebird-*.target \
                /etc/systemd/system/

log "installing sway config"
install -d /etc/sway/config.d
install -m 0644 "$LIVE_BUILD_INC/etc/sway/config.d/bluebird-locked.conf" /etc/sway/config.d/
# Stock sway config sources config.d/*; if it's missing, create a minimal one.
[[ -f /etc/sway/config ]] || \
  printf 'include /etc/sway/config.d/*\n' > /etc/sway/config
# Neutralize the stock Debian wallpaper line. It points at
# /usr/share/backgrounds/sway/Sway_Wallpaper_Blue_1920x1080.png from the
# `sway-backgrounds` package, which we don't install — so sway pops a red
# config-error nag bar over the kiosk on every boot. Our locked config
# already sets a solid black background, so just comment the line out.
if [[ -f /etc/sway/config ]]; then
  sed -i 's|^\(\s*output \* bg /usr/share/backgrounds/sway/.*\)|# &|' /etc/sway/config
fi

# Branded "Preparing your wall" loading wallpaper. bluebird-locked.conf points
# swaybg at this; it's what the operator sees between the Plymouth splash
# handing off and Chromium painting the wall (the launch-kiosk-chromium health
# wait + cloud probe can take several seconds). Static crest-on-navy that
# matches the animated boot splash, so the whole sequence reads as one piece.
if [[ -f "$LIVE_BUILD_INC/opt/bluebird-kiosk/share/loading.png" ]]; then
  log "installing kiosk loading wallpaper"
  install -d /opt/bluebird-kiosk/share
  install -m 0644 "$LIVE_BUILD_INC/opt/bluebird-kiosk/share/loading.png" \
                  /opt/bluebird-kiosk/share/loading.png
fi

log "installing kiosk launcher scripts"
install -d /opt/bluebird-kiosk/bin
install -m 0755 "$LIVE_BUILD_INC"/opt/bluebird-kiosk/bin/* /opt/bluebird-kiosk/bin/

log "installing managed Chromium policy"
# Belt-and-suspenders for the --disable-features= flags in
# launch-kiosk-chromium: the policy file applies even if the user-data
# dir is wiped or chromium changes which CLI feature names it honors.
# Crucially auto-allows the new Local Network Access permission API so
# the kiosk_cache shim (which fetches http://127.0.0.1:7311 from the
# HTTPS cloud page) doesn't trigger an "Access other apps and services
# on this device" prompt on Chromium 132+.
install -d /etc/chromium/policies/managed
install -m 0644 \
  "$LIVE_BUILD_INC/etc/chromium/policies/managed/bluebird-kiosk.json" \
  /etc/chromium/policies/managed/

# journald cap (+ persistent) and coredump-off drop-ins — keep an unattended kiosk from
# filling its disk over months of uptime. New files, so they must be applied on update too,
# not just baked into the ISO.
log "installing journald + coredump limits"
install -d /etc/systemd/journald.conf.d /etc/systemd/coredump.conf.d
install -m 0644 "$LIVE_BUILD_INC/etc/systemd/journald.conf.d/10-bluebird.conf" /etc/systemd/journald.conf.d/
install -m 0644 "$LIVE_BUILD_INC/etc/systemd/coredump.conf.d/10-bluebird.conf" /etc/systemd/coredump.conf.d/
systemctl restart systemd-journald 2>/dev/null || true

log "installing polkit rules"
install -d /etc/polkit-1/rules.d
install -m 0644 "$LIVE_BUILD_INC/etc/polkit-1/rules.d/49-bluebird-kiosk.rules" /etc/polkit-1/rules.d/
# polkit doesn't automatically pick up new rules from a running daemon, so the
# first reboot would get "Access denied" from systemctl reboot. Restart it now.
# Unit name varies (polkit.service vs polkitd.service); try both.
systemctl restart polkit.service 2>/dev/null \
  || systemctl restart polkitd.service 2>/dev/null \
  || true

log "installing default kiosk.conf (only if missing)"
if [[ ! -f /etc/bluebird/kiosk.conf ]]; then
  install -m 0640 -o bluebird-kiosk -g bluebird-kiosk \
    "$LIVE_BUILD_INC/etc/bluebird/kiosk.conf.example" /etc/bluebird/kiosk.conf
fi

log "handing all network interfaces to NetworkManager"
# Debian's installer sets up Ethernet via /etc/network/interfaces (ifupdown).
# NetworkManager respects that and marks the iface as "unmanaged", which
# breaks our firstboot wizard's Ethernet detection and prevents WiFi
# changes via the admin overlay. Take everything back over.
if [[ -f /etc/network/interfaces ]]; then
  if grep -qE '^\s*(auto|iface|allow-hotplug)\s+(eth|eno|enp|wlp|wlx|wls)' /etc/network/interfaces; then
    log "  rewriting /etc/network/interfaces to loopback-only"
    cp /etc/network/interfaces /etc/network/interfaces.pre-bluebird
    cat >/etc/network/interfaces <<'EOF'
# Managed by NetworkManager — see /etc/NetworkManager/conf.d/
auto lo
iface lo inet loopback
EOF
  fi
fi
install -d /etc/NetworkManager/conf.d
cat >/etc/NetworkManager/conf.d/10-globally-managed.conf <<'EOF'
[keyfile]
unmanaged-devices=none
EOF

log "configuring greetd (autologin → sway → kiosk)"
install -d /etc/greetd
# `initial_session` runs immediately on boot without showing a login prompt.
# `default_session` is required by greetd's config schema; we use the same
# command + user so that if sway ever exits, greetd respawns it directly
# rather than dropping to a greeter we don't ship.
cat >/etc/greetd/config.toml <<'EOF'
[terminal]
vt = 1

[initial_session]
command = "sway --config /etc/sway/config"
user = "bluebird-kiosk"

[default_session]
command = "sway --config /etc/sway/config"
user = "bluebird-kiosk"
EOF

# greetd owns the graphical session ONLY until the kiosk is configured. After
# firstboot writes /etc/bluebird/configured, bluebird-kiosk.service owns it (it
# runs sway directly) and greetd must stay out of the way. This condition plus
# the matching ConditionPathExists=/etc/bluebird/configured on
# bluebird-kiosk.service make the two mutually exclusive.
#
# WITHOUT this drop-in both start on every boot of a configured box —
# bluebird-gesture.service Wants= bluebird-kiosk.service, so the standby is
# pulled into the boot regardless — and two sway instances fight over DRM
# master: no frames reach any output, screens sit on the loading wallpaper or
# black while the heartbeat keeps reporting the device healthy. That took the
# NEN lobby kiosk down 2026-08-05/06. The live-build image has carried this
# file since #36 (2026-06-24); install.sh never did, so every Ubuntu-installed
# kiosk was exposed from that commit onward.
#
# Do NOT "simplify" this by masking greetd outright: on a fresh install the
# marker does not exist yet, bluebird-kiosk.service is condition-skipped, and
# with greetd gone there is no compositor at all — the box sits at a text
# console. That is the exact failure #36 fixed.
install -d /etc/systemd/system/greetd.service.d
cat >/etc/systemd/system/greetd.service.d/10-bluebird-firstboot.conf <<'EOF'
# BlueBird: greetd runs only while the kiosk is unconfigured (firstboot wizard).
# Mirror of build/live-build/.../greetd.service.d/10-bluebird-firstboot.conf —
# keep the two in sync.
[Unit]
ConditionPathExists=!/etc/bluebird/configured
EOF

# ── Unattended upgrades (security pocket only, 03:00 reboot window) ──────────
log "configuring unattended-upgrades"
cat >/etc/apt/apt.conf.d/52bluebird-unattended <<'EOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
};
# Automatic-Reboot OFF, deliberately. These are unattended school signage panels: an
# auto-reboot at 03:00 is uncoordinated with the fleet, invisible to the console, and lands
# the box in whatever latent boot bug it happens to be carrying. That is exactly how the
# greetd dual-compositor landmine armed on the NEN lobby kiosk (2026-08-05) — the box had
# run fine for days after the update and only broke on its first reboot.
#
# Security updates still INSTALL on schedule; they just apply on the next operator-initiated
# reboot (fleet console "Reboot", or bluebird-update). /var/run/reboot-required still gets
# written, and the heartbeat surfaces it, so a pending-reboot kiosk is visible rather than
# silently self-rebooting overnight.
Unattended-Upgrade::Automatic-Reboot "false";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# ── Enable services ──────────────────────────────────────────────────────────
log "enabling kiosk services + setting default target"
systemctl daemon-reload
systemctl enable NetworkManager.service
systemctl unmask greetd.service 2>/dev/null || true
systemctl enable greetd.service
# Self-heal a box that is ALREADY running the dual-compositor fault. The
# condition drop-in above only takes effect at the next start, so an updated
# box would otherwise stay broken until it reboots. Only act when the marker
# says this kiosk is configured AND bluebird-kiosk.service is genuinely the
# session owner — otherwise stopping greetd would kill the only compositor and
# blank the screen.
if [[ -e /etc/bluebird/configured ]] \
   && systemctl is-active --quiet bluebird-kiosk.service \
   && systemctl is-active --quiet greetd.service; then
  log "healing dual-compositor: stopping greetd (bluebird-kiosk.service owns the session)"
  systemctl stop greetd.service || true
fi
# Point /etc/systemd/system/display-manager.service at greetd explicitly —
# graphical.target Wants display-manager.service, and we want our DM to be
# greetd regardless of what other DM packages might leave behind.
ln -sf /usr/lib/systemd/system/greetd.service /etc/systemd/system/display-manager.service
# tty1 belongs to the graphical session (greetd's [initial_session], vt 1).
# getty.target brings up getty@tty1 by default, which races the session for the
# VT and briefly paints a text login prompt the instant Plymouth hands off —
# the "terminal login flash" the operator sees before the wall appears. Mask it
# so tty1 is the session's alone, in BOTH modes (unlike tty2-6, which stay as
# recovery consoles in debug and are only masked under --lockdown — tty1's getty
# is never wanted because the session always owns vt1). Field-verified on the NEN
# kiosk: getty@tty1 was in a ~50-restart loop fighting the session over tty1.
systemctl mask getty@tty1.service 2>/dev/null || true
systemctl enable bluebird-admin.service
systemctl enable bluebird-gesture.service
systemctl enable bluebird-heartbeat.service
systemctl enable bluebird-kiosk-sync.service
# Periodic auto-update check (every 6h, jittered 0-15min). The service it
# fires (bluebird-update.service) is itself NOT enabled — only triggered
# on demand by the timer or by the admin overlay's "Check for updates"
# button. Both paths go through bluebird-update which only runs install
# if the remote version differs from /etc/bluebird/kiosk-os.version, so
# the typical 6h tick is a cheap no-op.
systemctl enable bluebird-update.timer

# Restart the long-running Python services whose CODE this run just replaced.
#
# `systemctl enable` is a no-op on an already-enabled unit, so on an UPDATE (bluebird-update
# re-runs this script) the new code sat on disk while the OLD process kept running. That is
# not cosmetic: the heartbeat holds the remote-command executor map, so a freshly shipped
# command came back "unknown command" until the box happened to reboot. Field-confirmed
# 2026-08-07 — set_display_mode shipped, the kiosk reported it unknown, and the heartbeat
# process was 21 hours old.
#
# Restart rather than reload: these are plain Python services with no reload handler.
# `|| true` because on a FIRST install some of these have never started and a restart of a
# not-yet-running unit is fine to ignore.
log "restarting updated services so the new code is actually live"
for _svc in bluebird-heartbeat.service bluebird-admin.service bluebird-kiosk-sync.service bluebird-gesture.service; do
  if systemctl is-enabled --quiet "$_svc" 2>/dev/null; then
    systemctl restart "$_svc" 2>/dev/null || true
  fi
done
# Screenshot capture timer — fires every 60s, uploads a PNG of the
# current sway display to the fleet console. Pulls the license token
# from /etc/bluebird/license.token; silently skips if absent (pre-
# firstboot). See bluebird-screenshot.timer + take-screenshot script.
systemctl enable bluebird-screenshot.timer
# Display power scheduler — fires every 60s, applies the per-tenant on/off
# schedule (synced down by the heartbeat) via sway DPMS to conserve power
# outside configured hours. Fail-safe: leaves the display ON on any error and
# forces it ON during an active incident. See kiosk-power-scheduler + the
# bluebird-power-scheduler.{service,timer} units.
systemctl enable bluebird-power-scheduler.timer
# Reliability watchdog — polls sway/chromium/local-server every 30s,
# self-heals (restart greetd then reboot) on detected wedge. Closes
# KI-010 (kiosk goes offline after hours of uptime, required manual
# power-cycle). Runs as root because it has to call systemctl restart
# and systemctl reboot. See services/watchdog.py + the unit file.
systemctl enable bluebird-watchdog.service
systemctl enable unattended-upgrades.service
systemctl set-default bluebird-kiosk.target
# bluebird-kiosk.service and bluebird-firstboot.service are NOT enabled.
#   - sway is launched by greetd's session command, not by a standalone unit
#   - the firstboot wizard is picked by sway's launch-bluebird-session dispatcher
#     based on whether /etc/bluebird/configured exists
#
# Defensive disable: on upgrades from a pre-2026-06-04 build, the
# bluebird-kiosk.service unit had a [Install] section so systemd's
# preset machinery would auto-enable it. Together with greetd that
# caused a VT/DRM crash-loop. The new unit file has no [Install]
# block, but `disable` here also clears any leftover symlinks from
# the previous install state — idempotent + safe on fresh systems.
systemctl disable bluebird-kiosk.service 2>/dev/null || true

# ── SSH for remote management (always — operator + auto-update need it) ─────
# openssh-server is installed + enabled unconditionally now. The hardening
# pass below can selectively disable it when --lockdown is set without
# --keep-ssh; by default it stays on.
if ! dpkg -s openssh-server >/dev/null 2>&1; then
  log "installing openssh-server (default — remote management on by default)"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server
fi
systemctl enable --now ssh.service 2>/dev/null || systemctl enable --now sshd.service 2>/dev/null || true

# ── Harden (opt-in via --lockdown) ───────────────────────────────────────────
# Default behavior: leave VT switching reachable, root unlocked, ssh on,
# extra TTYs unmasked. Pass --lockdown for a kiosk that's going into a
# hallway or other unsupervised location.
if [[ "$LOCKDOWN" -eq 1 ]]; then
  log "applying lockdown (--lockdown)"
  install -d /etc/systemd/logind.conf.d
  cat >/etc/systemd/logind.conf.d/bluebird-kiosk.conf <<'EOF'
[Login]
NAutoVTs=0
ReserveVT=0
HandlePowerKey=ignore
HandleSuspendKey=ignore
HandleHibernateKey=ignore
HandleLidSwitch=ignore
KillUserProcesses=no
EOF
  systemctl mask getty@tty2.service getty@tty3.service getty@tty4.service \
    getty@tty5.service getty@tty6.service
  if [[ "$KEEP_SSH" -eq 1 ]]; then
    warn "lockdown + keep-ssh: harden applied, SSH left enabled for remote ops"
  else
    systemctl disable ssh.service 2>/dev/null || true
    warn "lockdown: SSH disabled — only Ctrl+Alt+T (PIN-gated) reaches a shell"
  fi
  passwd -l root || true
else
  log "no lockdown — SSH on, TTY switching reachable, root unlocked (default)"
  # Undo any residual lockdown from a prior --lockdown run on this same box,
  # so re-running the installer without --lockdown actually lifts it.
  rm -f /etc/systemd/logind.conf.d/bluebird-kiosk.conf
  systemctl unmask getty@tty2.service getty@tty3.service getty@tty4.service \
    getty@tty5.service getty@tty6.service 2>/dev/null || true
fi

# ── Boot splash (Plymouth) + GRUB tweaks ─────────────────────────────────────
# Plymouth shows the Legacy Wall badge from initramfs handoff through to
# greetd starting sway.
#
# Step 1: drop the bluebird theme files into the system Plymouth theme dir.
# (The earlier `install -m 0755 .../opt/bluebird-kiosk/bin/*` glob only
# touches /opt; theme files need their own copy.)
THEME_SRC="$LIVE_BUILD_INC/usr/share/plymouth/themes/bluebird"
if [[ -d "$THEME_SRC" ]]; then
  log "installing Plymouth theme files (bluebird)"
  install -d /usr/share/plymouth/themes/bluebird
  # Copy the whole theme dir so all assets ride along (the premium theme
  # uses background/glow/dot/emblem PNGs, not a single logo). Remove any
  # stale asset from an older theme version first so it can't linger.
  rm -f /usr/share/plymouth/themes/bluebird/logo.png
  install -m 0644 \
    "$THEME_SRC/bluebird.plymouth" \
    "$THEME_SRC/bluebird.script" \
    "$THEME_SRC/background.png" \
    "$THEME_SRC/glow.png" \
    "$THEME_SRC/dot.png" \
    "$THEME_SRC/emblem.png" \
    /usr/share/plymouth/themes/bluebird/
fi

# Step 2: activate the theme + rebuild initramfs. Two mechanisms:
#   - Debian: ships `plymouth-set-default-theme` (a small shell wrapper);
#     the -R flag also runs update-initramfs.
#   - Ubuntu 23.10+: dropped that binary in favor of update-alternatives.
#     We register bluebird.plymouth at high priority and set it active,
#     then update-initramfs by hand.
if [[ -d /usr/share/plymouth/themes/bluebird ]]; then
  log "activating Plymouth boot splash (theme: bluebird)"
  BLUEBIRD_PLY=/usr/share/plymouth/themes/bluebird/bluebird.plymouth
  if command -v plymouth-set-default-theme >/dev/null 2>&1; then
    plymouth-set-default-theme -R bluebird >/dev/null 2>&1 || \
      warn "  plymouth-set-default-theme failed — splash may not appear"
  elif command -v update-alternatives >/dev/null 2>&1; then
    update-alternatives --install \
      /usr/share/plymouth/themes/default.plymouth \
      default.plymouth \
      "$BLUEBIRD_PLY" 200 >/dev/null 2>&1
    update-alternatives --set default.plymouth "$BLUEBIRD_PLY" >/dev/null 2>&1
    if command -v update-initramfs >/dev/null 2>&1; then
      update-initramfs -u >/dev/null 2>&1 || \
        warn "  update-initramfs failed — splash may not appear until next regen"
    fi
  else
    warn "  no Plymouth theme-switch mechanism found (no plymouth-set-default-theme, no update-alternatives)"
  fi
else
  warn "  bluebird theme missing under /usr/share/plymouth/themes/ — skipping splash activation"
fi

# GRUB: hide the menu but keep it Esc-interruptible, narrow the timeout
# to 1s, pass `quiet splash` so Plymouth takes over the boot output, and
# stamp the distributor name so the boot menu entries read "BlueBird Kiosk"
# instead of "Debian" / "Ubuntu". All edits are idempotent (sed in place).
#
# i915.enable_psr=0 + i915.enable_fbc=0: a signage box reconfigures displays
# (hotplug, per-output modesets), and Intel Panel Self-Refresh / framebuffer
# compression are the classic cause of "[CRTC] flip_done timed out" + vblank
# WARNs on external panels. Disabling both makes Intel modesets reliable; the
# cost (a little more power / GPU bandwidth) is irrelevant on an always-on wall.
if [[ -f /etc/default/grub ]]; then
  log "configuring GRUB for silent splash boot"
  GRUB_TMP="$(mktemp)"
  cp /etc/default/grub "$GRUB_TMP"
  # quiet splash on the linux cmdline. Replace whatever's there (preserve
  # any other flags the operator added by appending if not already present).
  if grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$GRUB_TMP"; then
    sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="quiet splash i915.enable_psr=0 i915.enable_fbc=0"|' "$GRUB_TMP"
  else
    printf 'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash i915.enable_psr=0 i915.enable_fbc=0"\n' >> "$GRUB_TMP"
  fi
  # Hide the menu by default; Esc gets it back for recovery.
  if grep -q '^GRUB_TIMEOUT_STYLE=' "$GRUB_TMP"; then
    sed -i 's|^GRUB_TIMEOUT_STYLE=.*|GRUB_TIMEOUT_STYLE=hidden|' "$GRUB_TMP"
  else
    printf 'GRUB_TIMEOUT_STYLE=hidden\n' >> "$GRUB_TMP"
  fi
  if grep -q '^GRUB_TIMEOUT=' "$GRUB_TMP"; then
    sed -i 's|^GRUB_TIMEOUT=.*|GRUB_TIMEOUT=1|' "$GRUB_TMP"
  else
    printf 'GRUB_TIMEOUT=1\n' >> "$GRUB_TMP"
  fi
  if grep -q '^GRUB_DISTRIBUTOR=' "$GRUB_TMP"; then
    sed -i 's|^GRUB_DISTRIBUTOR=.*|GRUB_DISTRIBUTOR="BlueBird Kiosk"|' "$GRUB_TMP"
  else
    printf 'GRUB_DISTRIBUTOR="BlueBird Kiosk"\n' >> "$GRUB_TMP"
  fi
  install -m 0644 "$GRUB_TMP" /etc/default/grub
  rm -f "$GRUB_TMP"
  # `update-grub` is a Debian/Ubuntu wrapper for grub-mkconfig.
  if command -v update-grub >/dev/null 2>&1; then
    update-grub >/dev/null 2>&1 || warn "  update-grub returned non-zero"
  else
    grub-mkconfig -o /boot/grub/grub.cfg >/dev/null 2>&1 || \
      warn "  grub-mkconfig not found — boot menu config not refreshed"
  fi
fi

# ── Record installed version ─────────────────────────────────────────────────
# Stamp /etc/bluebird/kiosk-os.version with whatever the server currently
# advertises so the first auto-update tick is a no-op. Failure here is
# non-fatal (auto-update will re-detect on next tick and just self-heal).
# Source the kiosk.conf inside a subshell so we don't pollute this script's
# env. The file is plain KEY=value (no quotes), so no stripping is needed.
BACKEND_FOR_VERSION="$(
  if [[ -f /etc/bluebird/kiosk.conf ]]; then
    # shellcheck disable=SC1091
    source /etc/bluebird/kiosk.conf 2>/dev/null || true
  fi
  printf '%s' "${BLUEBIRD_BACKEND:-https://bluebird-alerts.com}"
)"
log "recording installed version (from ${BACKEND_FOR_VERSION})"
VERSION_JSON="$(curl -fsSL --max-time 10 \
  "${BACKEND_FOR_VERSION}/api/public/kiosk-os/version" 2>/dev/null || true)"
RECORDED_VERSION=""
if [[ -n "$VERSION_JSON" ]]; then
  # Parse JSON via python3 (always installed via apt list). Avoid jq —
  # not on the kiosk apt list.
  RECORDED_VERSION="$(printf '%s' "$VERSION_JSON" | /usr/bin/python3 -c \
    'import json, sys
try:
    print(json.load(sys.stdin).get("version", ""))
except Exception:
    pass' 2>/dev/null || true)"
fi
if [[ -n "$RECORDED_VERSION" ]]; then
  printf '%s\n' "$RECORDED_VERSION" > /etc/bluebird/kiosk-os.version
  log "  installed version: $RECORDED_VERSION"
else
  warn "  could not fetch version from server — auto-update will reconcile on first tick"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo
log "kiosk install complete."
echo
echo "Next steps:"
echo "  1. Reboot:  sudo reboot"
echo "  2. On reboot, the firstboot wizard will appear in fullscreen Chromium."
echo "     Walk through: WiFi (or skip for Ethernet) → school slug → 6-digit PIN."
echo "  3. After firstboot, the kiosk lands at the configured Legacy Wall URL."
echo
echo "Recovery:"
echo "  - Re-run this script without --lockdown to lift the harden for diagnostics."
echo "  - Re-run with --lockdown --keep-ssh for a hardened kiosk that still allows ssh/scp."
echo "  - PIN-gated terminal at the kiosk: Ctrl+Alt+T (enter the admin PIN)."
echo "  - Wipe & restart firstboot:  sudo rm /etc/bluebird/configured /etc/bluebird/admin.pin && sudo reboot"
echo
# SSH is on by default; surface the address. Suppressed only when --lockdown
# was applied AND --keep-ssh wasn't, which is the one combination that
# actually turns ssh off.
if [[ "$LOCKDOWN" -eq 0 || "$KEEP_SSH" -eq 1 ]]; then
  KIOSK_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "SSH: enabled — reach this kiosk with  ssh <user>@${KIOSK_IP:-<ip>}"
  echo
fi
