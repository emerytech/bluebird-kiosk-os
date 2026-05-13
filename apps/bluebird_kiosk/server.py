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

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import __version__, config
from .services import display, nmcli_wrapper, pin, system


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

    class WifiConnectBody(BaseModel):
        ssid: str = Field(..., min_length=1, max_length=64)
        password: Optional[str] = Field(default=None, max_length=128)

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

    class SlugBody(BaseModel):
        slug: str = Field(..., min_length=1, max_length=64)

    @app.post("/firstboot/slug/validate")
    async def firstboot_slug_validate(body: SlugBody):
        cfg = config.read_config()
        url = (
            cfg["BLUEBIRD_BACKEND"].rstrip("/")
            + "/api/public/legacy-wall/exists?slug="
            + body.slug.strip()
        )
        try:
            resp = requests.get(url, timeout=10)
            data = resp.json()
        except requests.RequestException as exc:
            return JSONResponse({"exists": False, "error": str(exc)}, status_code=502)
        return JSONResponse({"exists": bool(data.get("exists"))})

    class PinBody(BaseModel):
        pin: str = Field(..., min_length=6, max_length=6)
        confirm: str = Field(..., min_length=6, max_length=6)

    class FinalizeBody(BaseModel):
        slug: str = Field(..., min_length=1, max_length=64)
        pin: str = Field(..., min_length=6, max_length=6)

    @app.post("/firstboot/finalize")
    async def firstboot_finalize(body: FinalizeBody):
        cfg = config.read_config()
        legacy_url = config.derive_legacy_wall_url(
            cfg["BLUEBIRD_BACKEND"], body.slug
        )
        if not legacy_url:
            raise HTTPException(status_code=400, detail="Invalid backend or slug.")
        # Confirm the slug really exists & has Legacy Wall enabled.
        try:
            check = requests.get(
                cfg["BLUEBIRD_BACKEND"].rstrip("/")
                + "/api/public/legacy-wall/exists?slug="
                + body.slug.strip(),
                timeout=10,
            ).json()
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Backend unreachable: {exc}")
        if not check.get("exists"):
            raise HTTPException(status_code=400, detail="Unknown school slug.")

        try:
            pin.set_pin(body.pin)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        config.ensure_device_id()
        config.write_config(
            {
                "SCHOOL_SLUG": body.slug.strip(),
                "LEGACY_WALL_URL": legacy_url,
            }
        )
        config.mark_configured()
        return JSONResponse({"ok": True})

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

    class AdminLoginBody(BaseModel):
        pin: str = Field(..., min_length=6, max_length=6)

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

    class DisplayBody(BaseModel):
        output: str = Field(..., min_length=1, max_length=64)
        transform: Optional[str] = Field(default=None, pattern=r"^(normal|90|180|270)$")
        mode: Optional[str] = Field(default=None, max_length=32)
        brightness: Optional[int] = Field(default=None, ge=5, le=100)

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
        url = cfg["BLUEBIRD_BACKEND"].rstrip("/") + "/api/public/legacy-wall/exists?slug=" + body.slug.strip()
        try:
            check = requests.get(url, timeout=10).json()
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Backend unreachable: {exc}")
        if not check.get("exists"):
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

    class ChangePinBody(BaseModel):
        new_pin: str = Field(..., min_length=6, max_length=6)

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
