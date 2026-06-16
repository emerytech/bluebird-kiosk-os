"""On-device FastAPI control plane.

Bind: 127.0.0.1 only. Two surfaces:

  /firstboot/...  — setup wizard (no PIN; only reachable before configured flag)
  /admin/...      — PIN-locked device admin overlay (post-configuration)

A short-lived bearer token is issued on successful PIN entry and required for
every subsequent admin call. Tokens are kept in process memory; restarting
bluebird-admin.service invalidates them.
"""
from __future__ import annotations

import argparse
import logging
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

import os
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import __version__, config
from .services import display, nmcli_wrapper, pin, system
from .services.incident_poller import IncidentPoller
from .services.local_cache import KioskLocalCache
from .services.renderer import collect_slideshow_media, resolve_media_file_path


LOCAL_CACHE_DB = os.environ.get(
    "BLUEBIRD_LOCAL_CACHE_DB", "/var/lib/bluebird-kiosk/cache.db"
)


# ── Pydantic request bodies ───────────────────────────────────────────────────
# Defined at module scope, NOT inside create_app(): FastAPI introspects route
# signatures with typing.get_type_hints() which only sees module globals, so
# closure-local classes fall back to query-parameter parsing and the route
# always 422s with "field 'body' required".

class WifiConnectBody(BaseModel):
    ssid: str = Field(..., min_length=1, max_length=64)
    password: Optional[str] = Field(default=None, max_length=128)


class SlugBody(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64)


class LicenseBody(BaseModel):
    code: str = Field(..., min_length=8, max_length=32)


class PinBody(BaseModel):
    pin: str = Field(..., min_length=6, max_length=6)
    confirm: str = Field(..., min_length=6, max_length=6)


class FinalizeBody(BaseModel):
    pin: str = Field(..., min_length=6, max_length=6)


class AdminLoginBody(BaseModel):
    pin: str = Field(..., min_length=6, max_length=6)


class DisplayBody(BaseModel):
    output: str = Field(..., min_length=1, max_length=64)
    transform: Optional[str] = Field(default=None, pattern=r"^(normal|90|180|270)$")
    mode: Optional[str] = Field(default=None, max_length=32)
    brightness: Optional[int] = Field(default=None, ge=5, le=100)


class ChangePinBody(BaseModel):
    new_pin: str = Field(..., min_length=6, max_length=6)


class RunDiagnosticBody(BaseModel):
    diag_id: int = Field(..., ge=1)


# ── Media MIME sniffing ───────────────────────────────────────────────────────

def _sniff_media_type(path: str) -> str:
    """Detect image MIME from the file's first bytes.

    The kiosk-side sync_client stores media as <id>.bin (no extension),
    so FileResponse can't infer the type from the path and falls back to
    application/octet-stream — which chromium refuses to render in <img>
    tags. We sniff the magic bytes instead. Supports the formats the
    cloud Legacy Wall actually serves; falls back to image/jpeg so
    chromium still attempts a render rather than offering a download.
    """
    try:
        with open(path, "rb") as f:
            sig = f.read(16)
    except OSError:
        return "image/jpeg"
    if len(sig) >= 3 and sig[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(sig) >= 8 and sig[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(sig) >= 6 and sig[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(sig) >= 12 and sig[:4] == b"RIFF" and sig[8:12] == b"WEBP":
        return "image/webp"
    if len(sig) >= 12 and sig[4:12] == b"ftypavif":
        return "image/avif"
    return "image/jpeg"


# ── Backend slug existence check ──────────────────────────────────────────────

def slug_exists_remote(backend: str, slug: str) -> bool:
    """Check whether a school slug has a working Legacy Wall.

    Two strategies, tried in order:

    1. The dedicated `/api/public/legacy-wall/exists` endpoint. Most
       accurate — checks the legacy_wall add-on / product_mode, not just
       URL routing. Requires the backend to have public_kiosk_routes.py
       deployed.
    2. Fallback: GET the actual Legacy Wall URL. If it returns 200, the
       URL works, which is the only thing the kiosk actually cares about.
       Lets us deploy kiosks before the dedicated endpoint reaches
       production.
    """
    backend = backend.rstrip("/")
    slug = slug.strip()
    try:
        resp = requests.get(
            f"{backend}/api/public/legacy-wall/exists",
            params={"slug": slug},
            timeout=10,
        )
        if resp.status_code == 200:
            return bool(resp.json().get("exists"))
    except (requests.RequestException, ValueError):
        pass
    try:
        resp = requests.get(
            f"{backend}/{slug}/legacy-wall",
            timeout=10,
            allow_redirects=True,
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False


logger = logging.getLogger("bluebird-kiosk.server")

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "web" / "templates"))


def create_app() -> FastAPI:
    app = FastAPI(
        title="BlueBird Kiosk OS control plane",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.admin_sessions = {}
    # Single-slot store for the pending display revert. Holds the token
    # of the most recent /admin/display/apply that needs confirmation;
    # the auto-revert task checks this against its own token before
    # restoring. Cleared by /admin/display/confirm (operator approved)
    # or /admin/display/revert-now (manual revert). See DISPLAY_REVERT_SECONDS.
    app.state.display_revert_pending = {}
    app.state.local_cache = KioskLocalCache(LOCAL_CACHE_DB)
    # Mirrors this kiosk's active-incident state from the cloud. Created here but
    # STARTED in main() — not in create_app() — so importing the module (and the
    # test suite) never spins up a network thread.
    app.state.incident_poller = IncidentPoller()
    app.mount(
        "/static",
        StaticFiles(directory=str(HERE / "web" / "static")),
        name="static",
    )

    # ── Common ───────────────────────────────────────────────────────────────

    @app.get("/_health")
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/")
    async def root(request: Request):
        if config.is_configured():
            return templates.TemplateResponse(
                "admin_locked.html",
                {"request": request, "has_pin": pin.has_pin()},
            )
        return templates.TemplateResponse("firstboot.html", {"request": request})

    # ── First-boot wizard ────────────────────────────────────────────────────

    @app.get("/firstboot", response_class=HTMLResponse)
    async def firstboot_index(request: Request):
        if config.is_configured():
            return _redirect("/")
        return templates.TemplateResponse("firstboot.html", {"request": request})

    @app.get("/firstboot/wifi/scan")
    async def firstboot_wifi_scan():
        return JSONResponse(
            {
                "networks": [
                    {
                        "ssid": n.ssid,
                        "signal": n.signal,
                        "security": n.security,
                        "in_use": n.in_use,
                    }
                    for n in nmcli_wrapper.list_networks()
                ],
                "status": nmcli_wrapper.current_status(),
            }
        )

    @app.post("/firstboot/wifi/connect")
    async def firstboot_wifi_connect(body: WifiConnectBody):
        ok, msg = nmcli_wrapper.connect(body.ssid, body.password)
        return JSONResponse({"ok": ok, "message": msg})

    @app.post("/firstboot/slug/validate")
    async def firstboot_slug_validate(body: SlugBody):
        cfg = config.read_config()
        return JSONResponse(
            {"exists": slug_exists_remote(cfg["BLUEBIRD_BACKEND"], body.slug)}
        )

    @app.post("/firstboot/license/redeem")
    async def firstboot_license_redeem(body: LicenseBody):
        """Trade a license code for a bearer token. Persists the token to
        /etc/bluebird/license.token and records the slug + tenant name
        into kiosk.conf so the rest of the wizard knows which school we
        belong to.

        We deliberately don't mark the device 'configured' here — the user
        still has to set a PIN. Until then, /etc/bluebird/configured is
        absent and the wizard remains the active surface.
        """
        cfg = config.read_config()
        backend = (cfg.get("BLUEBIRD_BACKEND") or "").rstrip("/")
        if not backend:
            raise HTTPException(status_code=500, detail="Backend not configured.")
        device_id = config.ensure_device_id()
        try:
            resp = requests.post(
                f"{backend}/api/public/kiosk/license/redeem",
                json={"code": body.code, "device_id": device_id},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Network error: {exc}")
        if resp.status_code == 404:
            raise HTTPException(status_code=400, detail="Unknown or invalid code.")
        if resp.status_code == 410:
            # Either the license has been revoked or the tenant has been
            # archived. From the user's perspective the code just doesn't work.
            raise HTTPException(status_code=400, detail="License is no longer valid.")
        if resp.status_code == 409:
            raise HTTPException(
                status_code=400,
                detail="This code is bound to a different device.",
            )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=400,
                detail=f"Server rejected code (HTTP {resp.status_code}).",
            )
        try:
            payload = resp.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="Bad response from server.")

        token = payload.get("token") or ""
        slug = (payload.get("slug") or "").strip()
        tenant_name = payload.get("tenant_name") or ""
        if not token or not slug:
            raise HTTPException(status_code=502, detail="Bad response from server.")

        config.write_license_token(token)
        legacy_url = config.derive_legacy_wall_url(backend, slug)
        config.write_config(
            {
                "SCHOOL_SLUG": slug,
                "LEGACY_WALL_URL": legacy_url,
                "TENANT_NAME": tenant_name,
            }
        )
        return JSONResponse(
            {"ok": True, "slug": slug, "tenant_name": tenant_name}
        )

    @app.post("/firstboot/finalize")
    async def firstboot_finalize(body: FinalizeBody):
        # By the time we get here, the license redeem step has already
        # persisted SCHOOL_SLUG + LEGACY_WALL_URL into kiosk.conf and
        # written the bearer token to /etc/bluebird/license.token. We just
        # need a PIN + the 'configured' flag.
        cfg = config.read_config()
        if not (cfg.get("SCHOOL_SLUG") and cfg.get("LEGACY_WALL_URL")):
            raise HTTPException(status_code=400, detail="Redeem a license first.")
        if not config.has_license():
            raise HTTPException(status_code=400, detail="Redeem a license first.")
        try:
            pin.set_pin(body.pin)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        config.mark_configured()
        return JSONResponse({"ok": True})

    # ── Local Legacy Wall renderer ───────────────────────────────────────────
    # v0: reads from KioskLocalCache (synced by bluebird-kiosk-sync.service)
    # and serves a minimal fullscreen slideshow. Chromium is pointed here in
    # offline-first mode so the kiosk keeps showing content even when the
    # network is down.

    @app.get("/legacy-wall", response_class=HTMLResponse)
    async def legacy_wall_home(request: Request):
        cfg = config.read_config()
        return templates.TemplateResponse(
            "legacy_wall.html",
            {
                "request": request,
                "tenant_name": cfg.get("TENANT_NAME") or cfg.get("SCHOOL_SLUG") or "",
                "version": __version__,
            },
        )

    @app.get("/legacy-wall/api/media")
    async def legacy_wall_api_media(request: Request):
        cache: KioskLocalCache = request.app.state.local_cache
        media = collect_slideshow_media(cache)
        return JSONResponse({"media": media, "count": len(media)})

    @app.get("/legacy-wall/api/active-incident")
    async def legacy_wall_active_incident(request: Request):
        """Latest active-incident state for THIS kiosk's tenant, cached by the
        background IncidentPoller (which polls the cloud with the license token).
        The cloud Legacy Wall page polls this over loopback to drive its
        full-screen incident modal, so the page never holds the token. Returns
        {"active": false} when not enrolled / nothing active / poller absent."""
        poller = getattr(request.app.state, "incident_poller", None)
        if poller is None:
            return JSONResponse({"active": False, "is_active": False})
        return JSONResponse(poller.current())

    @app.get("/legacy-wall/media/{media_id}")
    async def legacy_wall_media_blob(request: Request, media_id: int):
        cache: KioskLocalCache = request.app.state.local_cache
        file_path = resolve_media_file_path(cache, int(media_id))
        if not file_path or not os.path.isfile(file_path):
            raise HTTPException(status_code=404, detail="media_not_cached")
        return FileResponse(
            path=file_path,
            media_type=_sniff_media_type(file_path),
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/legacy-wall/media/{media_id}/{variant}")
    async def legacy_wall_media_blob_with_variant(
        request: Request, media_id: int, variant: str
    ):
        """Match the cloud Legacy Wall's image URL shape so a Chromium
        extension can redirect them to us 1:1.

        Cloud emits image src URLs like:
            /<slug>/legacy-wall/media/<id>/image
            /<slug>/legacy-wall/media/<id>/thumb

        The bluebird_kiosk_cache_ext extension (a tiny MV3 redirect
        rule) rewrites those to:
            http://127.0.0.1:7311/legacy-wall/media/<id>/image
            http://127.0.0.1:7311/legacy-wall/media/<id>/thumb

        Behavior here:
          • variant=image OR thumb — we serve the same on-disk bytes
            for both. The browser scales the bitmap for thumb spots.
            Slightly more bandwidth than a true thumb but no extra
            sync storage. (sync_client only downloads /image variants.)
          • cache miss — 302 redirect to the cloud URL, so a missing
            item still appears in the dashboard, just slower for that
            single fetch. Operator-confirmed behavior 2026-06-04.

        Any variant other than 'image'/'thumb' is rejected — the cloud
        only ever asks for those two.
        """
        if variant not in ("image", "thumb"):
            raise HTTPException(status_code=404, detail="unknown_variant")
        cache: KioskLocalCache = request.app.state.local_cache
        file_path = resolve_media_file_path(cache, int(media_id))
        # Treat a 0-byte cache file as a miss. sync_client.py SHOULD never
        # leave 0-byte files (we now verify size before recording the blob)
        # but the field saw 20 such files at NEN on 2026-06-05 from a
        # pre-fix sync run. Without this check the route would return
        # `200 OK` with an empty body → chromium renders a broken image
        # in the dashboard for that specific photo.
        if file_path and os.path.isfile(file_path):
            try:
                size_ok = os.path.getsize(file_path) > 0
            except OSError:
                size_ok = False
            if size_ok:
                return FileResponse(
                    path=file_path,
                    media_type=_sniff_media_type(file_path),
                    headers={"Cache-Control": "private, max-age=86400"},
                )
        # Cache miss — fall through to the cloud. The kiosk Chromium has
        # WAN to bluebird-alerts.com working at this point (we're serving
        # cached media, but if any specific item didn't sync yet the
        # cloud is the source of truth). Reconstruct the cloud URL from
        # the kiosk.conf so it stays correct across tenant changes.
        cfg = config.read_config()
        backend = (cfg.get("BLUEBIRD_BACKEND") or "").rstrip("/")
        slug = (cfg.get("SCHOOL_SLUG") or "").strip("/")
        if not backend or not slug:
            raise HTTPException(status_code=404, detail="no_backend_for_fallback")
        cloud_url = f"{backend}/{slug}/legacy-wall/media/{int(media_id)}/{variant}"
        return RedirectResponse(url=cloud_url, status_code=302)

    # ── Admin overlay (PIN-gated) ────────────────────────────────────────────

    def require_admin(
        request: Request,
        x_admin_session: Optional[str] = Header(default=None),
    ) -> None:
        if not pin.admin_session_valid(x_admin_session, request.app.state.admin_sessions):
            raise HTTPException(status_code=401, detail="Admin session required.")

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_index(request: Request):
        if not config.is_configured():
            return _redirect("/firstboot")
        return templates.TemplateResponse(
            "admin.html",
            {"request": request, "version": __version__},
        )

    @app.post("/admin/login")
    async def admin_login(request: Request, body: AdminLoginBody):
        if pin.is_locked_out():
            return JSONResponse(
                {
                    "ok": False,
                    "error": "locked_out",
                    "retry_after_seconds": pin.lockout_seconds_remaining(),
                },
                status_code=429,
            )
        if not pin.verify_pin(body.pin):
            return JSONResponse({"ok": False, "error": "invalid_pin"}, status_code=401)
        token = secrets.token_urlsafe(24)
        request.app.state.admin_sessions[token] = time.monotonic() + 30 * 60
        return JSONResponse({"ok": True, "session_token": token})

    @app.post("/admin/logout")
    async def admin_logout(
        request: Request,
        x_admin_session: Optional[str] = Header(default=None),
    ):
        if x_admin_session:
            request.app.state.admin_sessions.pop(x_admin_session, None)
        return JSONResponse({"ok": True})

    # Network
    @app.get("/admin/network/status", dependencies=[Depends(require_admin)])
    async def admin_network_status():
        return JSONResponse(
            {
                "status": nmcli_wrapper.current_status(),
                "networks": [
                    {
                        "ssid": n.ssid,
                        "signal": n.signal,
                        "security": n.security,
                        "in_use": n.in_use,
                    }
                    for n in nmcli_wrapper.list_networks()
                ],
            }
        )

    @app.post("/admin/network/connect", dependencies=[Depends(require_admin)])
    async def admin_network_connect(body: WifiConnectBody):
        ok, msg = nmcli_wrapper.connect(body.ssid, body.password)
        return JSONResponse({"ok": ok, "message": msg})

    # Display
    @app.get("/admin/display/outputs", dependencies=[Depends(require_admin)])
    async def admin_display_outputs():
        return JSONResponse(
            {
                "outputs": [
                    {
                        "name": o.name,
                        "enabled": o.enabled,
                        "current_mode": o.current_mode,
                        "modes": o.available_modes,
                        "transform": o.transform,
                    }
                    for o in display.list_outputs()
                ]
            }
        )

    # Auto-revert window for rotation/mode changes — modeled on Windows'
    # display settings dialog. Operator applies a change, gets 15 seconds
    # to confirm; otherwise the previous state is restored. Without this,
    # rotating the kiosk wrong leaves the operator unable to reach the
    # admin overlay to undo it (field-confirmed 2026-06-04 — required
    # an SSH hop to fix a Smart Board stuck at 90°).
    DISPLAY_REVERT_SECONDS = 15

    async def _auto_revert_display(token: str, output: str, prev_transform: str, prev_mode: str):
        import asyncio as _asyncio
        await _asyncio.sleep(DISPLAY_REVERT_SECONDS)
        # If the operator confirmed (or replaced with another change),
        # our token is no longer the pending one — bail.
        if app.state.display_revert_pending.get("token") != token:
            return
        try:
            if prev_transform:
                display.set_rotation(output, prev_transform)
            if prev_mode:
                display.set_mode(output, prev_mode)
        finally:
            app.state.display_revert_pending.pop("token", None)

    @app.post("/admin/display/apply", dependencies=[Depends(require_admin)])
    async def admin_display_apply(body: DisplayBody):
        messages = []
        # Capture current state BEFORE applying so we can revert. Only
        # needed when transform or mode changes — brightness can't trap
        # the operator, so we don't auto-revert it.
        revert_needed = body.transform is not None or body.mode is not None
        prev_transform = ""
        prev_mode = ""
        if revert_needed:
            for o in display.list_outputs():
                if o.name == body.output:
                    prev_transform = o.transform
                    prev_mode = o.current_mode
                    break

        if body.transform is not None:
            ok, msg = display.set_rotation(body.output, body.transform)
            messages.append(f"rotation: {msg}")
            if not ok:
                return JSONResponse({"ok": False, "messages": messages}, status_code=400)
        if body.mode is not None:
            ok, msg = display.set_mode(body.output, body.mode)
            messages.append(f"mode: {msg}")
            if not ok:
                return JSONResponse({"ok": False, "messages": messages}, status_code=400)
        if body.brightness is not None:
            ok, msg = display.set_brightness(body.brightness)
            messages.append(f"brightness: {msg}")
            if not ok:
                return JSONResponse({"ok": False, "messages": messages}, status_code=400)

        # Schedule the auto-revert if rotation/mode changed.
        revert_in = 0
        if revert_needed:
            token = secrets.token_urlsafe(12)
            app.state.display_revert_pending = {
                "token": token,
                "output": body.output,
                "prev_transform": prev_transform,
                "prev_mode": prev_mode,
                "deadline": time.monotonic() + DISPLAY_REVERT_SECONDS,
            }
            import asyncio as _asyncio
            _asyncio.create_task(_auto_revert_display(
                token, body.output, prev_transform, prev_mode
            ))
            revert_in = DISPLAY_REVERT_SECONDS

        return JSONResponse({
            "ok": True,
            "messages": messages,
            "revert_in_seconds": revert_in,
        })

    @app.post("/admin/display/confirm", dependencies=[Depends(require_admin)])
    async def admin_display_confirm():
        """Operator clicked 'Keep changes' on the auto-revert banner.
        Clear the pending revert so the timer task is a no-op."""
        had_pending = app.state.display_revert_pending.pop("token", None) is not None
        # Drop the rest of the dict too.
        app.state.display_revert_pending.clear()
        return JSONResponse({"ok": True, "was_pending": had_pending})

    @app.post("/admin/display/revert-now", dependencies=[Depends(require_admin)])
    async def admin_display_revert_now():
        """Operator clicked 'Revert' to undo the last display change
        immediately. Restores the captured previous state and clears
        the pending revert (so the auto-revert timer task no-ops)."""
        p = dict(app.state.display_revert_pending)
        if not p.get("token"):
            return JSONResponse({"ok": False, "error": "nothing_to_revert"}, status_code=400)
        app.state.display_revert_pending.clear()  # neutralize the auto-revert task
        messages = []
        if p.get("prev_transform"):
            ok, msg = display.set_rotation(p["output"], p["prev_transform"])
            messages.append(f"rotation: {msg}")
        if p.get("prev_mode"):
            ok, msg = display.set_mode(p["output"], p["prev_mode"])
            messages.append(f"mode: {msg}")
        return JSONResponse({"ok": True, "messages": messages})

    # Kiosk control
    @app.get("/admin/kiosk/state", dependencies=[Depends(require_admin)])
    async def admin_kiosk_state():
        cfg = config.read_config()
        return JSONResponse(
            {
                "slug": cfg.get("SCHOOL_SLUG"),
                "url": cfg.get("LEGACY_WALL_URL"),
                "device_id": cfg.get("DEVICE_ID"),
                "backend": cfg.get("BLUEBIRD_BACKEND"),
                "version": __version__,
            }
        )

    @app.post("/admin/kiosk/restart", dependencies=[Depends(require_admin)])
    async def admin_kiosk_restart():
        ok, msg = system.restart_kiosk()
        return JSONResponse({"ok": ok, "message": msg})

    # ── Cross-origin entry point for the cloud Legacy Wall ──────────────────
    #
    # The cloud Legacy Wall (https://bluebirdalerts.com/<slug>/legacy-wall),
    # running in the kiosk Chromium window, exposes a "Kiosk Settings"
    # button in its settings menu when:
    #   1. its admin-mode PIN gate is active, AND
    #   2. its window's User-Agent contains "BlueBirdKiosk" (set by our
    #      Chromium launcher).
    #
    # Clicking it POSTs here. We don't reuse `require_admin` because the
    # local admin cookie is bound to the loopback origin and the request
    # crosses origins. The launched overlay has its own PIN gate, so the
    # surface this exposes to a (hypothetical) cross-origin attacker is
    # "the same thing that a 5-finger gesture or Ctrl+Alt+B would do" —
    # i.e. open a PIN prompt. CORS is locked to the canonical cloud host.
    _CORS_ORIGINS = {
        "https://bluebirdalerts.com",
        "https://www.bluebirdalerts.com",
        "https://bluebird-alerts.com",
        "https://www.bluebird-alerts.com",
        # White-label LW domain — kiosks point chromium at this so the
        # branded display doesn't say "bluebird-alerts.com". The Kiosk
        # Settings cross-origin POST originates from here, so it MUST
        # be in the allowlist or the browser silently drops the POST
        # after the OPTIONS preflight succeeds (no Allow-Origin header
        # → fetch hangs, no .then / .catch fires; eventually .catch
        # produces "Kiosk admin server unreachable").
        "https://legacy.ets3d.com",
    }

    def _cors_headers(request: Request) -> Dict[str, str]:
        origin = (request.headers.get("origin") or "").lower()
        if origin in _CORS_ORIGINS:
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600",
                # Chrome's Private Network Access (PNA) restriction blocks
                # HTTPS-page → loopback fetches unless the preflight response
                # explicitly opts in. Without this header, the "Kiosk Settings"
                # button on the cloud Legacy Wall fails silently in browser-land
                # even though every server-side CORS check passes from curl.
                # The kiosk admin server is *the* only loopback target the
                # cloud page ever talks to, so blanket-allowing is safe.
                "Access-Control-Allow-Private-Network": "true",
                "Vary": "Origin",
            }
        return {}

    @app.options("/admin/kiosk/open-overlay")
    async def admin_open_overlay_preflight(request: Request):
        return JSONResponse({"ok": True}, headers=_cors_headers(request))

    @app.post("/admin/kiosk/open-overlay")
    async def admin_open_overlay(request: Request):
        # Issue a single-use 5-minute session so the operator doesn't
        # have to enter the local kiosk PIN — they already authenticated
        # in the cloud BlueBird admin to reach the Kiosk Settings button.
        # The 5-minute TTL is shorter than a regular PIN-issued session
        # (30 min) because this skipped the device-local auth step.
        preauth = secrets.token_urlsafe(24)
        request.app.state.admin_sessions[preauth] = time.monotonic() + 5 * 60
        ok, msg = system.open_admin_overlay(preauth_token=preauth)
        if not ok:
            # Roll back the token so it can't be guessed via the URL later.
            request.app.state.admin_sessions.pop(preauth, None)
        status = 200 if ok else 500
        return JSONResponse(
            {"ok": ok, "message": msg},
            status_code=status,
            headers=_cors_headers(request),
        )

    @app.post("/admin/kiosk/return-to-kiosk", dependencies=[Depends(require_admin)])
    async def admin_return_to_kiosk():
        """One-click recovery / dismissal action used by the admin overlay:
        relaunch the slideshow Chromium window (fixes a stuck blank page)
        AND close the admin overlay window the user is sitting in. Fire
        the reload first so the slideshow is up before the overlay closes
        and our HTTP connection to the admin server dies."""
        reload_ok, reload_msg = system.reload_kiosk_display()
        # Best-effort close; ignore the result since the response will
        # die mid-flight when our own Chromium process is killed.
        system.close_admin_overlay()
        return JSONResponse(
            {"ok": reload_ok, "reload": reload_msg, "overlay_closed": True}
        )

    @app.post("/admin/kiosk/slug", dependencies=[Depends(require_admin)])
    async def admin_kiosk_slug(body: SlugBody):
        cfg = config.read_config()
        if not slug_exists_remote(cfg["BLUEBIRD_BACKEND"], body.slug):
            raise HTTPException(status_code=400, detail="Unknown school slug.")
        new_url = config.derive_legacy_wall_url(cfg["BLUEBIRD_BACKEND"], body.slug)
        config.write_config({"SCHOOL_SLUG": body.slug.strip(), "LEGACY_WALL_URL": new_url})
        return JSONResponse({"ok": True, "url": new_url})

    # System
    @app.post("/admin/system/reboot", dependencies=[Depends(require_admin)])
    async def admin_system_reboot():
        ok, msg = system.reboot()
        return JSONResponse({"ok": ok, "message": msg})

    @app.post("/admin/system/shutdown", dependencies=[Depends(require_admin)])
    async def admin_system_shutdown():
        ok, msg = system.shutdown()
        return JSONResponse({"ok": ok, "message": msg})

    @app.get("/admin/system/logs", dependencies=[Depends(require_admin)])
    async def admin_system_logs(lines: int = 200):
        return PlainTextResponse(system.recent_logs(lines))

    @app.post("/admin/system/check-updates", dependencies=[Depends(require_admin)])
    async def admin_check_updates():
        """Kick off the oneshot bluebird-update.service. Returns immediately;
        the UI polls /admin/system/update-status to follow progress."""
        ok, msg = system.start_update()
        return JSONResponse({"ok": ok, "message": msg})

    # ── Console tab (diagnostic allow-list) ──────────────────────────────────

    @app.get("/admin/system/diagnostics", dependencies=[Depends(require_admin)])
    async def admin_list_diagnostics():
        """Return the locally-cached diagnostic allow-list for the Console tab."""
        return JSONResponse({"diagnostics": system.list_diagnostics_for_console()})

    @app.post("/admin/system/diagnostics/refresh", dependencies=[Depends(require_admin)])
    async def admin_refresh_diagnostics():
        """Force a fresh pull from the BlueBird server so the operator can
        see server-side edits without waiting for the next sync tick."""
        return JSONResponse(system.refresh_diagnostics_from_backend())

    @app.post("/admin/system/diagnostics/run", dependencies=[Depends(require_admin)])
    async def admin_run_diagnostic(body: RunDiagnosticBody):
        """Execute one diagnostic by id. The runner enforces:
          • id must be present in the locally-cached allow-list, AND
          • the cached command must still pass the regex guard.
        See services/system.py::run_diagnostic for the trust model."""
        return JSONResponse(system.run_diagnostic(int(body.diag_id)))

    @app.get("/admin/system/update-status", dependencies=[Depends(require_admin)])
    async def admin_update_status(lines: int = 80):
        """Current state of bluebird-update.service + recent journal output."""
        return JSONResponse(system.update_status(lines))

    @app.post("/admin/system/change-pin", dependencies=[Depends(require_admin)])
    async def admin_change_pin(body: ChangePinBody):
        try:
            pin.set_pin(body.new_pin)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse({"ok": True})

    @app.post("/admin/system/factory-reset", dependencies=[Depends(require_admin)])
    async def admin_factory_reset():
        ok, msg = system.factory_reset()
        if ok:
            system.reboot()
        return JSONResponse({"ok": ok, "message": msg})

    return app


def _redirect(target: str) -> HTMLResponse:
    return HTMLResponse(
        f'<html><head><meta http-equiv="refresh" content="0; url={target}"></head></html>',
        status_code=200,
    )


app = create_app()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7311)
    args = parser.parse_args()
    import uvicorn

    # Start the background cloud poll only now (real run) — never at import time.
    app.state.incident_poller.start()
    uvicorn.run(app, host=args.bind, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
