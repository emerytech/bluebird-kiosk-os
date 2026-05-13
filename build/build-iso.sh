#!/usr/bin/env bash
# Build the BlueBird Kiosk OS installer ISO.
#
# The actual build runs inside a Debian Bookworm Docker container with
# live-build + debootstrap installed. On macOS, debootstrap CANNOT write to
# a bind-mounted host directory (Docker Desktop's file-share layer blocks
# mknod). So we copy the source tree into the container's local filesystem,
# build there, and copy the finished ISO back out.
#
# Output:  dist/bluebird-kiosk-os.iso
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

mkdir -p "$HERE/dist"

# Reproducible build timestamp.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(date -u +%s)}"

if [[ "${IN_BUILDER:-0}" != "1" ]]; then
  echo "==> Building builder image (one-time, cached afterwards)"
  docker build --platform=linux/amd64 \
    -f "$HERE/Dockerfile.builder" \
    -t bluebird-kiosk-os-builder \
    "$HERE"

  echo "==> Starting build container"
  CID="$(docker run -d --rm --privileged --platform=linux/amd64 \
    -e IN_BUILDER=1 \
    -e SOURCE_DATE_EPOCH \
    bluebird-kiosk-os-builder \
    sleep infinity)"
  trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT

  echo "==> Copying source tree into container"
  docker exec "$CID" mkdir -p /work
  # tar-pipe avoids file permission weirdness from `docker cp` on macOS.
  tar -C "$ROOT" -cf - apps build | docker exec -i "$CID" tar -C /work -xf -

  echo "==> Running live-build inside container"
  docker exec -e IN_BUILDER=1 -e SOURCE_DATE_EPOCH "$CID" \
    bash -c 'cd /work/build && ./build-iso.sh'

  echo "==> Copying ISO out"
  docker cp "$CID:/work/build/dist/." "$HERE/dist/"

  echo "==> Done. Artifacts in $HERE/dist/"
  ls -lh "$HERE/dist"
  exit 0
fi

# --- Inside the builder container from here ----------------------------------

cd "$HERE/live-build"

echo "==> live-build clean"
lb clean --purge || true

echo "==> live-build config"
lb config \
  --architectures amd64 \
  --binary-images iso-hybrid \
  --bootappend-live "boot=live components quiet splash" \
  --debian-installer false \
  --distribution bookworm \
  --image-name "bluebird-kiosk-os" \
  --iso-application "BlueBird Kiosk OS Installer" \
  --iso-volume "BLUEBIRD-KIOSK" \
  --apt-indices false \
  --memtest none

echo "==> live-build build"
lb build

mkdir -p "$HERE/dist"
mv ./*.iso "$HERE/dist/" 2>/dev/null || true
mv ./*.iso.* "$HERE/dist/" 2>/dev/null || true

echo "==> Done. Artifacts in $HERE/dist/"
ls -lh "$HERE/dist"
