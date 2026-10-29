# Photo cache correctness — review handoff

Source-only changes from BlueBird main `72082000` and kiosk-os main `9fd47944`.
No deployment, migration, production photo reads/writes, screen assignment changes,
VPS sizing, alarms or external notifications. All reproduction data is synthetic.

## Contract and implementation

* Each successful bearer-authenticated manifest is a consistent SQLite snapshot.
  `snapshot_tables` explicitly declares complete inventories. The new client only
  reconciles absent IDs for those declared tables; an omitted delta row is never
  inferred to be a delete. Full snapshots avoid tombstone expiry during long
  disconnections. A malformed/failed pull cannot authorize a purge or cursor advance.
* Published media, published non-archived albums, and published collections are the
  kiosk display scope. Related rows with media/album/collection foreign keys are
  filtered to that scope. Public image/thumb requests independently check all
  three publication levels and tenant ownership. Draft listing requires dashboard
  authentication and returns authenticated preview URLs; public photo routes never
  use an admin cookie as a publication bypass.
* `media_version` fingerprints all original/WebP/thumbnail paths plus filesystem
  size, nanosecond mtime/ctime and inode. Caption-only edits do not force native
  byte downloads. Storage replacement under an existing ID, including in-place
  same-length replacement, changes the version without relying on updated_at.
* Photo downloads use sibling temporary files, fsync, length/size checks and optional
  SHA-256 verification, then atomic replace. Source metadata must still match before
  recording a blob. Missing/empty/truncated/version-stale files never send old ETags;
  an unexpected304 is retried unconditionally once and never counts as repaired.
  HTML error pages, non-identity encodings and responses above64MiB are refused.
* Mirror updates and cursor commit together. Reconciliation purges blob indexes and
  numeric photo files; orphan cleanup retries after interruption, including on local
  client recreation without WAN. Deleting a cache symlink never follows its target.
* Mutable photo URLs include `?v=...`; the cloud shim and Chromium redirect preserve
  that version. A local version mismatch falls back to the cloud instead of returning
  old bytes under a new URL. New public/local byte responses are private/no-store.
* One shared path resolver handles bare, nested, absolute-in-root and newer
  `data/legacy-wall/<slug>/...` paths; cross-school, traversal and escaping symlinks
  are refused. No changes to incident auth/polling/activation, takeover scripts,
  notification transports, background-video sync or live configuration.

## Delivery and storage assessment before object storage

The cloud thumb route serves an existing <=480px derivative (with original fallback).
Native kiosk `thumb` still serves its cached full original. This work does NOT claim
that native thumbnail bandwidth/decode cost is solved. A follow-up variant protocol
must key `(media_id, variant, version)`, download true thumbnails, bound decode size,
and test old-kiosk compatibility before switching delivery/storage.

Photo orphan cleanup and a64MiB per-download cap are NOT an aggregate cache budget.
The native photo and separate background/rotation video caches still need an explicit
combined byte quota, minimum free-space reserve, crash-safe accounting and a reviewed
working-set eviction policy that avoids a cold-download/eviction loop. No arbitrary
capacity setting or broad eviction is introduced here. Benchmark cold/warm galleries,
true thumbnails, manifest JSON size/latency and disk pressure on an isolated kiosk.
Full metadata snapshots trade additional small JSON/stat traffic for unbounded-offline
hard-delete correctness; content hashing occurs on media delivery, not each manifest.

Keep the VPS and current media source intact. The audit measured only about1.82GB of
wall files; moving them does not justify downsizing. Database files, OCR, quota,
backup custody, derivatives and video paths still need explicit migration design.

## Rollout and limitations

Review companion kiosk/backend changes together. New kiosks tolerate old delta
manifests (without hard-delete reconciliation); old kiosks tolerate snapshot upserts
but cannot remove absent IDs. Both updates are needed for full correctness. The
BlueBird submodule remains pinned to released main until the companion PR is reviewed
and merged; do not silently roll out an unreviewed kiosk build.

Previously downloaded browser images and a truly disconnected kiosk cannot be
remotely erased. Reconnection plus a successful snapshot removes local metadata and
photo disk files. New versioned URLs avoid prior long-lived browser entries; physical
Chromium cache/update acceptance and actual school-device offline/reboot testing
remain required. Backups are outside this cache deletion contract. This is not a
claim of all-copy data erasure or DPA signature readiness.

PR721 overlaps Legacy publication checks and PR724 overlaps shared media handling;
retain their additional Beacon/video/import protections when reconciling. No PR was
merged and no deletion/retention executor was enabled by this work.

## Local validation

* Backend Python3.9.6:118 passed, including real bearer sync routes, publication/path
  regression fixtures, tenant isolation, hotfix regressions and the Node incident
  poll behavior wrapper. Outbound Python networking/DNS was fenced; all databases
  and uploaded images were synthetic. Test-only ReportLab<5 and HTTPX<0.28 constraints
  were used; production dependency declarations/installations were not changed.
* Final backend scope/path/visibility checks:54 passed after the final shared-path
  normalization and administration-scope review (overlaps the broad118 checks).
* Kiosk Python3.13:94 passed with FastAPI0.110.0/HTTPX0.27.2, including cache recovery,
  local renderer, launcher signage, incident poller and loopback CORS. These are the
  audit-compatible versions. The previously documented unbounded-dependency/template
  compatibility issue remains separate; do not interpret this as latest-dependency
  or physical-kiosk certification.
* Cross-repository probe passes replacement under the same ID/path/size/timestamp,
  empty-file repair, actual store hard-delete plus reconnect, and other-tenant
  preservation. The portable driver is retained with the local audit evidence.
* Full backend CI and physical display/offline/reboot/load acceptance remain separate
  review gates. This work does not activate alarms or send notifications.
