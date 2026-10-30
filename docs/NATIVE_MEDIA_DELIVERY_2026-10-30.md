# Native thumbnails and bounded media cache — October30,2026

Source-only follow-up to companion drafts BlueBird741 / kiosk-os51. Backend integrated current GitHub main9696fee8, preserving editor744. Kiosk main remains9fd47944. Existing isolated branches reused; original checkouts/edits and backend kiosk submodule pin preserved. No deployment, merge, migration, VPS change, school data writes, alarms, sends, live assignment change, Helios action, or retention/deletion executor activation.

## Delivery contract

The authenticated native media endpoint defaults to original bytes for old clients. `?variant=thumb` returns a fresh, metadata-free WebP at most480x480, content checksum/ETag, media revision and explicit `X-Media-Variant: thumb`. The manifest advertises supported `media_variants`; new clients do not ask old servers for an unnegotiated thumbnail. Originals and thumbnails use independent SQLite indexes and files. Missing/replaced/truncated bytes retry unconditionally and atomically; a thumbnail failure does not purge a good original.

A bounded thumbnail is rendered from the current original, avoiding a stale derivative after replacement. JPEG draft decode reduces dimensions before load; remaining decode is limited to20million pixels, input64MiB, output2MiB. Invalid/oversized/unsupported originals fail closed, with no full-image thumbnail substitution. Generation is serialized per process; capacity is awaited before dispatch to the shared thread pool to avoid parking unrelated workers behind thumbnail jobs. No source image is rewritten or migrated.

New public thumbnail listing links and native fallback add `bounded=1`. That path rechecks the same tenant/entitlement/media/album/collection publication rules before generating a bounded thumbnail. Unflagged legacy paths retain compatibility. Authenticated admin preview currently still uses its full original preview URL; that separate gallery optimization is not claimed complete. Deploy backend support before any eventual new kiosk rollout; mixed old backends can still use legacy fallback behavior. Both draft PRs remain held for coordinated review and physical acceptance.

## Native cache policy

Candidate defaults, not settings applied to a live kiosk:

```
MEDIA_CACHE_MAX_BYTES=4294967296
MEDIA_CACHE_MIN_FREE_BYTES=1073741824
```

These integer kiosk.conf options bound combined native original/thumbnail/designated-video/rotation-video bytes. The cache database, Chromium's browser cache, logs, OS and unrelated files are outside this byte quota; free-space checks protect against those other consumers but cannot control them. Existing Chromium launcher already specifies a100MiB browser disk cache. Physical filesystem/capacity acceptance is still required.

Admission reserves peak extra bytes, keeping the old file until replacement completes. Missing Content-Length requires a checksum and reserves the per-blob ceiling (2MiB thumb,64MiB original,512MiB video); conservative admission can defer a file that might fit once its real size is known. Free space is checked before admission and during writes; competing non-cache writers can still consume space. Excess cache bytes are evicted on sync startup, not by a new timer or live setting change.

A deterministic working set prioritizes thumbnails, designated background clips, originals, then rotation clips; lower IDs win within a tier. Only lower-priority files can be evicted for a new item. Over-capacity libraries do not redownload and evict the same bodies every tick. The tradeoff is explicit: the whole library/rotation is not guaranteed offline; over-budget misses fall back to cloud when connected. There is no active-frame pinning or LRU claim. Policy/defaults require operator review on a spare kiosk before rollout.

Only known native cache filenames directly inside the configured media/video roots are removable. Deletion never trusts an absolute index path or follows symlink targets; a symlinked lock is refused. A cross-process writer lock covers admission, temporary files and commit; owned crash staging is cleaned under that lock. Filesystem sizes provide accounting even when indexes outlive eviction/crash. No photo metadata, pending events, source files or school records are evicted by the byte budget. Old SQLite cache schemas upgrade additively.

Photo and video writes are atomic and length/checksum validated. A missing or invalid video cannot use an old ETag to accept304 as repair. Partial/error/HTML/encoded/oversized video responses keep old bytes. Per-tick attempts are bounded per variant so failing thumbnails cannot starve original downloads. Incident polling, alert authentication/activation and takeover scripts are unchanged.

## Evidence and limitations

- Backend broad run:127 passed,2 failed due only to empty/uninitialized released kiosk submodule. After populating that unchanged submodule, both tarball/version fixtures passed. Final overlapping focused suite:44 passed, including new header-bomb and asynchronous decode-capacity cases. No tests removed/skipped; one existing Python3.9 support warning.
- Kiosk final:109 passed,26 dependency deprecation warnings, including old cache upgrade, variant repair, stable over-capacity working set, combined photo/video accounting, free-space refusal, crash staging, symlink safety, atomic video repair, cloud fallback, incident poll/launcher/CORS and stubbed independent-cast preemption. Tested with audit-compatible FastAPI0.110.0/HTTPX0.27.2, not a claim of latest unbounded dependency compatibility.
- Cross-repository synthetic SQLite + actual manifest/byte handler/native client: replacement, empty original and thumb repair, offline restart, actual store hard-delete/reconnect and other-tenant preservation passed under an outbound Python connection/DNS fence. Seeded1200x800 original726143bytes → thumb60620bytes (about92percent smaller); warm media download0bytes. One fixture, not a production bandwidth/latency estimate.
- No physical spare kiosk is available. Reboot/WAN loss/Chromium redirect/range playback/disk pressure/active content and incident-overlay field acceptance remain unperformed. A complete cloud wall offline is not claimed. A disconnected device and prior browser copies cannot be erased immediately.
- Full new-head backend GitHub CI is required. Old6aee1f20green is stale after this update. Kiosk repository currently reports no GitHub check workflow; local tests are not a physical release gate substitute.

Review both PRs together, including overlapping protections from721/724. Keep object storage, real photo migration, VPS downsizing and production/kiosk release on hold. Do not change the backend submodule pin to this unreviewed kiosk head.
