#!/usr/bin/env bash
# BlueBird Kiosk OS — Ubuntu 24.04 Server autoinstall ISO builder.
#
# Downloads the stock Ubuntu Server ISO, embeds the autoinstall config
# (user-data + meta-data) at /nocloud/ inside the ISO, patches the GRUB
# boot config to enable autoinstall via the NoCloud cloud-init data
# source, and rebuilds the ISO with xorriso. Output:
#
#   dist/bluebird-kiosk-ubuntu-24.04-<short-git-sha>.iso
#
# Usage:
#   ./build-iso.sh                       # uses pinned UBUNTU_VERSION
#   UBUNTU_VERSION=24.04.2 ./build-iso.sh  # pick a specific point release
#   SKIP_DOWNLOAD=1 ./build-iso.sh         # reuse cached source ISO
#
# Requires: xorriso, isolinux (or syslinux), wget, sha256sum
#   apt install -y xorriso isolinux wget
#
# Tested on: Ubuntu 24.04 host, Debian 13 host. CI: GitHub Actions
# ubuntu-latest (see .github/workflows/build-autoinstall-iso.yml).

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
UBUNTU_VERSION="${UBUNTU_VERSION:-24.04.1}"
UBUNTU_ARCH="${UBUNTU_ARCH:-amd64}"
SOURCE_ISO_NAME="ubuntu-${UBUNTU_VERSION}-live-server-${UBUNTU_ARCH}.iso"
SOURCE_ISO_URL="https://releases.ubuntu.com/${UBUNTU_VERSION}/${SOURCE_ISO_NAME}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CACHE_DIR="${BB_KIOSK_CACHE:-$HOME/.cache/bluebird-kiosk-os}"
WORK_DIR="$(mktemp -d -t bb-kiosk-iso.XXXXXX)"
DIST_DIR="$SCRIPT_DIR/dist"

SHORT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
OUTPUT_ISO="$DIST_DIR/bluebird-kiosk-ubuntu-${UBUNTU_VERSION}-${SHORT_SHA}.iso"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

cleanup() {
  if [[ -n "${WORK_DIR:-}" && -d "$WORK_DIR" ]]; then
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

# ── Preflight ─────────────────────────────────────────────────────────────────
for cmd in xorriso wget sha256sum; do
  command -v "$cmd" >/dev/null 2>&1 || \
    die "missing required tool: $cmd  (apt install xorriso wget)"
done

mkdir -p "$CACHE_DIR" "$DIST_DIR"

# ── Step 1: download source ISO (cached) ─────────────────────────────────────
SOURCE_ISO="$CACHE_DIR/$SOURCE_ISO_NAME"
if [[ "${SKIP_DOWNLOAD:-0}" == "1" && -f "$SOURCE_ISO" ]]; then
  log "using cached $SOURCE_ISO_NAME (SKIP_DOWNLOAD=1)"
elif [[ -f "$SOURCE_ISO" ]] && \
     [[ -f "$CACHE_DIR/${SOURCE_ISO_NAME}.sha256" ]] && \
     ( cd "$CACHE_DIR" && sha256sum -c "${SOURCE_ISO_NAME}.sha256" >/dev/null 2>&1 ); then
  log "cached $SOURCE_ISO_NAME passed checksum — reusing"
else
  log "downloading $SOURCE_ISO_NAME (~3 GB)"
  wget --quiet --show-progress -O "$SOURCE_ISO" "$SOURCE_ISO_URL"
  ( cd "$CACHE_DIR" && sha256sum "$SOURCE_ISO_NAME" > "${SOURCE_ISO_NAME}.sha256" )
fi

# ── Step 2: extract ISO contents ─────────────────────────────────────────────
log "extracting ISO to $WORK_DIR/iso"
mkdir -p "$WORK_DIR/iso"
xorriso -osirrox on -indev "$SOURCE_ISO" -extract / "$WORK_DIR/iso" >/dev/null 2>&1

# `osirrox` extracts with non-writable perms; flip them so we can edit.
chmod -R u+w "$WORK_DIR/iso"

# ── Step 3: inject autoinstall files ─────────────────────────────────────────
log "embedding autoinstall user-data + meta-data"
mkdir -p "$WORK_DIR/iso/nocloud"
cp "$SCRIPT_DIR/autoinstall/user-data" "$WORK_DIR/iso/nocloud/user-data"
cp "$SCRIPT_DIR/autoinstall/meta-data" "$WORK_DIR/iso/nocloud/meta-data"

# ── Step 4: patch GRUB config to autoinstall + auto-pick the first entry ─────
# The Ubuntu Server 24.04 ISO uses /boot/grub/grub.cfg for both UEFI and BIOS
# (no isolinux). We replace it with a tiny config that boots straight into the
# autoinstall flow via the NoCloud datasource at /cdrom/nocloud/.
log "patching GRUB config for autoinstall boot"
GRUB_CFG="$WORK_DIR/iso/boot/grub/grub.cfg"
[[ -f "$GRUB_CFG" ]] || die "expected GRUB config at boot/grub/grub.cfg — Ubuntu ISO layout changed?"

# Detect the existing kernel + initrd paths (Ubuntu has occasionally moved
# them between casper/ and casper-something/). Grab from the original cfg.
KERNEL_PATH="$(grep -oE '/casper/vmlinuz[a-zA-Z0-9.-]*' "$GRUB_CFG" | head -1)"
INITRD_PATH="$(grep -oE '/casper/initrd[a-zA-Z0-9.-]*' "$GRUB_CFG" | head -1)"
[[ -n "$KERNEL_PATH" && -n "$INITRD_PATH" ]] || \
  die "couldn't find kernel/initrd paths in the upstream GRUB config"

log "  kernel: $KERNEL_PATH"
log "  initrd: $INITRD_PATH"

cat > "$GRUB_CFG" <<GRUB
# BlueBird Kiosk OS autoinstall image — replaces the upstream Ubuntu Server
# GRUB menu. Boots straight into Subiquity in autoinstall mode, with the
# NoCloud datasource pointing at /cdrom/nocloud/ where our user-data lives.

set timeout=2
set default=0

# Match Ubuntu's terminal config so the early-boot output renders.
if loadfont /boot/grub/font.pf2 ; then
    set gfxmode=auto
    insmod efi_gop
    insmod efi_uga
    insmod gfxterm
    terminal_output gfxterm
fi

menuentry "BlueBird Kiosk OS — autoinstall (default)" {
    set gfxpayload=keep
    linux  $KERNEL_PATH quiet autoinstall ds=nocloud;s=/cdrom/nocloud/  ---
    initrd $INITRD_PATH
}

menuentry "Try or Install Ubuntu Server (manual)" {
    set gfxpayload=keep
    linux  $KERNEL_PATH quiet  ---
    initrd $INITRD_PATH
}

menuentry "Boot from next volume" {
    exit
}

menuentry "UEFI Firmware Settings" {
    fwsetup
}
GRUB

# isolinux/legacy BIOS: Ubuntu 24.04 server ISOs don't ship a usable isolinux
# tree — they're GRUB-only via El Torito for both UEFI and BIOS. So no
# isolinux patch needed; GRUB handles both.

# ── Step 5: rebuild the ISO ──────────────────────────────────────────────────
# Use the same boot record + El Torito layout the upstream ISO uses, so the
# rebuilt image stays bootable on both UEFI and legacy BIOS. The boot image
# offsets are read straight from the source ISO via xorriso — no hand-tuning.
log "rebuilding ISO → $OUTPUT_ISO"

# Pull the original El Torito boot image so we can reuse it. xorriso's
# `-boot_image any replay` does this transparently when we point at the
# source ISO as a template.
xorriso -as mkisofs -r \
  -V "BlueBird Kiosk OS $UBUNTU_VERSION" \
  -J -joliet-long \
  -l \
  -iso-level 3 \
  -o "$OUTPUT_ISO" \
  -partition_offset 16 \
  --grub2-mbr "$WORK_DIR/iso/boot/grub/i386-pc/boot_hybrid.img" \
  --mbr-force-bootable \
  -append_partition 2 0xef "$WORK_DIR/iso/EFI/boot/bootx64.efi.img" 2>/dev/null \
    || true   # newer Ubuntu ISOs don't ship the EFI image as a flat file

# Fallback path: most reliable across Ubuntu 24.04.x releases is to use
# xorriso's `-indev` template mode which copies the upstream boot layout
# byte-for-byte and just substitutes the changed files.
if [[ ! -s "$OUTPUT_ISO" ]] || ! file "$OUTPUT_ISO" 2>/dev/null | grep -q "boot sector"; then
  log "  primary mkisofs path didn't produce a hybrid bootable image — falling back to template mode"
  rm -f "$OUTPUT_ISO"
  xorriso -indev "$SOURCE_ISO" \
    -outdev "$OUTPUT_ISO" \
    -boot_image any replay \
    -volid "BlueBird Kiosk OS $UBUNTU_VERSION" \
    -map "$WORK_DIR/iso/nocloud" /nocloud \
    -map "$WORK_DIR/iso/boot/grub/grub.cfg" /boot/grub/grub.cfg \
    -commit 2>&1 | tail -5
fi

# ── Step 6: hash + report ────────────────────────────────────────────────────
log "computing SHA-256"
( cd "$DIST_DIR" && sha256sum "$(basename "$OUTPUT_ISO")" | tee "$(basename "$OUTPUT_ISO").sha256" )

SIZE_MB=$(( $(stat -c%s "$OUTPUT_ISO" 2>/dev/null || stat -f%z "$OUTPUT_ISO") / 1024 / 1024 ))
echo
log "BUILD COMPLETE"
log "  output: $OUTPUT_ISO"
log "  size:   ${SIZE_MB} MB"
log "  flash:  sudo dd if=$OUTPUT_ISO of=/dev/sdX bs=4M status=progress oflag=sync"
log "          (or use balenaEtcher / Ventoy / Rufus)"
echo
