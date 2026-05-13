# Building BlueBird Kiosk OS

Engineer-facing guide. Builds a single x86_64 `.iso` installer image.

## Build paths

| Where | How | When |
|---|---|---|
| **GitHub Actions** | Push to `kiosk-os/**` or trigger `Build Kiosk OS ISO` manually. | Production. The canonical build. |
| **Local Linux (x86_64)** | `IN_BUILDER=1 ./build-iso.sh` directly on a Debian/Ubuntu host. | When iterating fast and you have one. |
| **Local macOS via Docker** | `./build-iso.sh` (wraps live-build in a Debian container). | **Does not work on Apple Silicon** — cross-arch debootstrap fails inside Docker Desktop's QEMU emulation. Use CI or a real Linux box. |

### Why CI is the right answer

`debootstrap` extracts a Debian base, then runs `chroot <target> /bin/true` to
verify the chroot is healthy. On Apple Silicon, Docker Desktop emulates amd64
via qemu-user-static, and the nested `chroot` interpreter lookup fails. There
is no clean workaround short of running on a real x86_64 kernel. The CI
workflow at `.github/workflows/build-kiosk-os.yml` runs on a vanilla
`ubuntu-latest` runner — amd64 native, no nesting, no QEMU.

## Build via CI

1. Push your changes to a branch that touches `kiosk-os/`.
2. The **Build Kiosk OS ISO** workflow runs automatically.
3. When green, download the `bluebird-kiosk-os-iso` artifact from the run.
4. To publish: cut a GitHub release; the workflow re-runs and attaches the
   ISO + SHA256SUMS to the release.

To trigger a one-off build without pushing: GitHub → Actions → "Build Kiosk
OS ISO" → Run workflow.

## Build via local Linux

```bash
# On Debian 12 / Ubuntu 22.04+ (amd64), as root:
sudo apt-get install -y live-build debootstrap squashfs-tools xorriso \
    isolinux syslinux-common grub-efi-amd64-bin grub-pc-bin mtools dosfstools

cd kiosk-os/build
sudo IN_BUILDER=1 ./build-iso.sh
```

Output: `kiosk-os/build/dist/bluebird-kiosk-x86_64.iso`. First build ~25 min,
subsequent builds faster because live-build caches the bootstrap.

## Test in QEMU

```bash
cd kiosk-os/build/dist
qemu-img create -f qcow2 test.qcow2 16G
qemu-system-x86_64 \
  -m 4G -enable-kvm \
  -cdrom bluebird-kiosk-x86_64.iso \
  -drive file=test.qcow2,if=virtio \
  -netdev user,id=net0 -device virtio-net,netdev=net0 \
  -display sdl
```

Walk Calamares, install, reboot, then:

- The first-boot wizard should appear automatically.
- After completing it, the VM reboots into Chromium pointed at
  `https://bluebird-alerts.com/<your-slug>/legacy-wall`.

Five-finger long-press is hard to test in QEMU; simulate it with `evemu`:

```bash
# inside the VM, as root
evemu-event /dev/input/event<touchscreen-N> --type EV_ABS --code ABS_MT_SLOT --value 0
# ... see kiosk-os/docs/SIMULATE_GESTURE.md (TODO)
```

In practice, easier to verify on real hardware.

## Modifying the image

- **Add a package** — append to
  `build/live-build/config/package-lists/bluebird-kiosk.list.chroot`. Keep this
  list minimal; every package is attack surface.
- **Tweak the kiosk Chromium flags** — edit
  `build/live-build/config/includes.chroot/opt/bluebird-kiosk/bin/launch-kiosk-chromium`.
- **Change admin overlay tools** — edit `apps/bluebird_kiosk/` (routes are in
  `server.py`, services in `services/`). Rebuild the ISO to ship.
- **Change the gesture (e.g. four-finger long-press)** — edit
  `apps/bluebird_gesture/__main__.py` (constants near the top).

## Backend dependency

The OS calls these endpoints on the BlueBird backend:

- `GET  /api/public/legacy-wall/exists?slug=<slug>`
- `POST /api/public/kiosk/heartbeat`

Both live in `backend/app/api/public_kiosk_routes.py`. Both bypass tenant
middleware and resolve `tenant_id` from the slug via `tenant_manager`.
`tenant_id` is never accepted from the request body.

## Release

- Tag: `kiosk-os-vX.Y.Z`
- Build the ISO, compute its SHA-256, upload both to the marketing CDN at
  `https://bluebird-alerts.com/downloads/kiosk-os/latest/`.
- Update `kiosk-os/docs/INSTALL.md` only if the install steps changed.
