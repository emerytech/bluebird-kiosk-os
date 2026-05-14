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
#   --debug          Skip the lockdown step (VTs stay accessible, root stays
#                    usable). Use during development. Re-run without --debug to
#                    apply the harden later.
#   --no-firstboot   Don't auto-launch the firstboot wizard on next reboot.
#                    Use if you want to drop the device into kiosk mode via
#                    a pre-baked /etc/bluebird/kiosk.conf.
#
# Idempotent: safe to re-run. Each step checks state before acting.

set -euo pipefail

# ── Locate the repo root (this script is at kiosk-os/install/install.sh) ─────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${KIOSK_REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
KIOSK_OS="$REPO_ROOT/kiosk-os"
LIVE_BUILD_INC="$KIOSK_OS/build/live-build/config/includes.chroot"

# ── Flags ─────────────────────────────────────────────────────────────────────
DEBUG=0
SKIP_FIRSTBOOT=0
for arg in "$@"; do
  case "$arg" in
    --debug) DEBUG=1 ;;
    --no-firstboot) SKIP_FIRSTBOOT=1 ;;
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
  die "this script only supports Debian 12 / Ubuntu 24.04 — refusing to run on $(grep ^PRETTY_NAME /etc/os-release)"
fi
. /etc/os-release
case "${VERSION_ID:-}" in
  12|24.04) ;;
  *) warn "running on ${ID} ${VERSION_ID:-unknown} (only 12 / 24.04 are tested) — proceeding anyway" ;;
esac

log "kiosk install starting (repo: $REPO_ROOT, debug=$DEBUG, firstboot=$((1 - SKIP_FIRSTBOOT)))"

# ── apt packages ─────────────────────────────────────────────────────────────
log "installing apt packages (this is the slow step)"

# Ensure non-free-firmware is enabled (Intel/Realtek firmware lives there)
if ! grep -q 'non-free-firmware' /etc/apt/sources.list 2>/dev/null; then
  log "  enabling non-free-firmware in sources.list"
  sed -i 's|^\(deb .*\(main\|main contrib\)\)$|\1 non-free non-free-firmware|' /etc/apt/sources.list || true
fi

DEBIAN_FRONTEND=noninteractive apt-get update -qq

# Mirror of kiosk-os/build/live-build/config/package-lists/bluebird-kiosk.list.chroot,
# minus things only relevant inside a live ISO build (debian-installer, etc.)
APT_PACKAGES=(
  # Display stack
  sway swayidle swaylock seatd xwayland
  mesa-vulkan-drivers libgl1-mesa-dri

  # Greeter
  greetd

  # Browser
  chromium

  # Networking
  network-manager wpasupplicant iw dnsutils iputils-ping

  # Display utilities
  brightnessctl wlr-randr

  # Input + gesture daemon dependencies
  libinput-tools python3-evdev

  # Python runtime for our app
  python3 python3-pip python3-venv
  python3-fastapi python3-uvicorn python3-jinja2 python3-pydantic
  python3-bcrypt python3-requests

  # Firmware for common WiFi/eth chips
  firmware-linux firmware-iwlwifi firmware-realtek firmware-misc-nonfree

  # Maintenance
  sudo systemd systemd-timesyncd unattended-upgrades ca-certificates

  # Used by the install script itself
  git rsync
)

DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"

# ── User + directories ───────────────────────────────────────────────────────
log "creating bluebird-kiosk system group + user (UID/GID 1000)"
if ! getent group bluebird-kiosk >/dev/null; then
  groupadd --gid 1000 bluebird-kiosk
fi
if ! id bluebird-kiosk >/dev/null 2>&1; then
  useradd --uid 1000 --gid 1000 \
    --home-dir /var/lib/bluebird-kiosk --shell /usr/sbin/nologin \
    --no-create-home bluebird-kiosk
fi
install -d -o bluebird-kiosk -g bluebird-kiosk /var/lib/bluebird-kiosk
install -d -o bluebird-kiosk -g bluebird-kiosk /etc/bluebird
# Allow kiosk user to read its own /run dir.
install -d -m 0700 -o bluebird-kiosk -g bluebird-kiosk /run/user/1000 || true

# ── Python packages ──────────────────────────────────────────────────────────
log "installing bluebird_kiosk + bluebird_gesture Python packages"
install -d /opt/bluebird-kiosk/src
rsync -a --delete "$KIOSK_OS/apps/bluebird_kiosk/" /opt/bluebird-kiosk/src/bluebird_kiosk/
rsync -a --delete "$KIOSK_OS/apps/bluebird_gesture/" /opt/bluebird-kiosk/src/bluebird_gesture/

pip3 install --break-system-packages --no-cache-dir /opt/bluebird-kiosk/src/bluebird_kiosk
pip3 install --break-system-packages --no-cache-dir /opt/bluebird-kiosk/src/bluebird_gesture

# ── Drop /etc and /opt files ─────────────────────────────────────────────────
log "installing systemd units"
install -m 0644 "$LIVE_BUILD_INC"/etc/systemd/system/bluebird-*.service \
                "$LIVE_BUILD_INC"/etc/systemd/system/bluebird-*.target \
                /etc/systemd/system/

log "installing sway config"
install -d /etc/sway/config.d
install -m 0644 "$LIVE_BUILD_INC/etc/sway/config.d/bluebird-locked.conf" /etc/sway/config.d/
# Stock sway config sources config.d/*; if it's missing, create a minimal one.
[[ -f /etc/sway/config ]] || \
  printf 'include /etc/sway/config.d/*\n' > /etc/sway/config

log "installing kiosk launcher scripts"
install -d /opt/bluebird-kiosk/bin
install -m 0755 "$LIVE_BUILD_INC"/opt/bluebird-kiosk/bin/* /opt/bluebird-kiosk/bin/

log "installing polkit rules"
install -d /etc/polkit-1/rules.d
install -m 0644 "$LIVE_BUILD_INC/etc/polkit-1/rules.d/49-bluebird-kiosk.rules" /etc/polkit-1/rules.d/

log "installing default kiosk.conf (only if missing)"
if [[ ! -f /etc/bluebird/kiosk.conf ]]; then
  install -m 0640 -o bluebird-kiosk -g bluebird-kiosk \
    "$LIVE_BUILD_INC/etc/bluebird/kiosk.conf.example" /etc/bluebird/kiosk.conf
fi

log "configuring greetd (autologin → sway → kiosk)"
install -d /etc/greetd
cat >/etc/greetd/config.toml <<'EOF'
[terminal]
vt = 1

[default_session]
command = "sway --config /etc/sway/config"
user = "bluebird-kiosk"
EOF

# ── Unattended upgrades (security pocket only, 03:00 reboot window) ──────────
log "configuring unattended-upgrades"
cat >/etc/apt/apt.conf.d/52bluebird-unattended <<'EOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
};
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# ── Enable services ──────────────────────────────────────────────────────────
log "enabling kiosk services + setting default target"
systemctl daemon-reload
systemctl enable NetworkManager.service
systemctl unmask greetd.service 2>/dev/null || true
systemctl enable greetd.service
systemctl enable bluebird-admin.service
[[ "$SKIP_FIRSTBOOT" -eq 0 ]] && systemctl enable bluebird-firstboot.service
systemctl enable bluebird-kiosk.service
systemctl enable bluebird-gesture.service
systemctl enable bluebird-heartbeat.service
systemctl enable unattended-upgrades.service
systemctl set-default bluebird-kiosk.target

# ── Harden (skipped under --debug) ───────────────────────────────────────────
if [[ "$DEBUG" -eq 0 ]]; then
  log "applying lockdown (skip with --debug)"
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
  systemctl disable ssh.service 2>/dev/null || true
  passwd -l root || true
else
  warn "debug mode: VT switching, root, ssh, and getty@tty[2-6] left as-is"
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
echo "  - Re-run this script with --debug to lift the lockdown for diagnostics."
echo "  - Wipe & restart firstboot:  sudo rm /etc/bluebird/configured /etc/bluebird/admin.pin && sudo reboot"
echo
