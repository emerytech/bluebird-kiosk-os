#!/usr/bin/env bash
# BlueBird Kiosk OS — in-place patch script for an already-built ISO.
#
# When you change *only* the autoinstall config (iso-builder/autoinstall/*)
# you don't need to redownload the 3 GB Ubuntu Server ISO or re-extract
# the whole tree. xorriso's template mode reads the existing ISO and
# overwrites just the changed files, preserving the boot layout. ~30 sec
# vs. the 5–10 min full rebuild in build-iso.sh.
#
# Usage:
#   ./patch-iso.sh <input.iso> [output.iso]
#
# If output.iso is omitted, defaults to <input>-patched.iso next to the
# input. The original file is left untouched.
#
# Limits: this ONLY swaps files that already exist at known paths inside
# the ISO. If you added a new file to /nocloud/ or changed the GRUB boot
# entries, run build-iso.sh instead.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $# -ge 1 ]] || die "usage: $0 <input.iso> [output.iso]"
INPUT="$1"
[[ -f "$INPUT" ]] || die "input ISO not found: $INPUT"

if [[ $# -ge 2 ]]; then
  OUTPUT="$2"
else
  # foo.iso → foo-patched.iso
  OUTPUT="${INPUT%.iso}-patched.iso"
fi

command -v xorriso >/dev/null 2>&1 || \
  die "xorriso not found  (brew install xorriso  /  apt install xorriso)"

USER_DATA="$SCRIPT_DIR/autoinstall/user-data"
META_DATA="$SCRIPT_DIR/autoinstall/meta-data"
[[ -f "$USER_DATA" ]] || die "missing $USER_DATA"
[[ -f "$META_DATA" ]] || die "missing $META_DATA"

log "patching $INPUT"
log "  → $OUTPUT"
log "  user-data: $(wc -c <"$USER_DATA") bytes"

# Copy input to output, then mutate in place — keeps the original ISO
# intact in case something goes wrong.
cp "$INPUT" "$OUTPUT"

# `-indev` + `-outdev` on the same path = read-modify-write the ISO.
# `-boot_image any replay` preserves the El Torito boot record so the
# patched ISO stays bootable on both UEFI and legacy BIOS.
xorriso \
  -indev "$OUTPUT" \
  -outdev "$OUTPUT" \
  -boot_image any replay \
  -map "$USER_DATA" /nocloud/user-data \
  -map "$META_DATA" /nocloud/meta-data \
  -commit 2>&1 | tail -8

# SHA-256 alongside.
if command -v shasum >/dev/null 2>&1; then
  ( cd "$(dirname "$OUTPUT")" && shasum -a 256 "$(basename "$OUTPUT")" \
      | tee "$(basename "$OUTPUT").sha256" )
elif command -v sha256sum >/dev/null 2>&1; then
  ( cd "$(dirname "$OUTPUT")" && sha256sum "$(basename "$OUTPUT")" \
      | tee "$(basename "$OUTPUT").sha256" )
fi

SIZE_MB=$(( $(stat -f%z "$OUTPUT" 2>/dev/null || stat -c%s "$OUTPUT") / 1024 / 1024 ))
echo
log "PATCH COMPLETE"
log "  output: $OUTPUT"
log "  size:   ${SIZE_MB} MB"
log "  flash:  sudo dd if=$OUTPUT of=/dev/diskN bs=4m status=progress"
log "          (Mac: diskutil list  → find the USB stick → diskutil unmountDisk /dev/diskN first)"
