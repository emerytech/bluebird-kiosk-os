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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import __version__, config
from .services import display, nmcli_wrapper, pin, system
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
    app.state.local_cache = KioskLocalCache(LOCAL_CACHE_DB)
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

    @app.get("/legacy-wall/media/{media_id}")
    async def legacy_wall_media_blob(request: Request, media_id: int):
        cache: KioskLocalCache = request.app.state.local_cache
        file_path = resolve_media_file_path(cache, int(media_id))
        if not file_path or not os.path.isfile(file_path):
            raise HTTPException(status_code=404, detail="media_not_cached")
        return FileResponse(
            path=file_path,
            headers={"Cache-Control": "private, max-age=86400"},
        )

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

    @app.post("/admin/display/apply", dependencies=[Depends(require_admin)])
    async def admin_display_apply(body: DisplayBody):
        messages = []
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

    uvicorn.run(app, host=args.bind, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
