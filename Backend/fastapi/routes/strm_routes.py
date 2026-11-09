"""
FastAPI routes for Emby / Jellyfin .strm export.

Endpoints:
  GET /api/strm/export.zip        - Download all .strm + .nfo files as a single zip.
  POST /api/strm/sync             - Trigger sync to local disk (configured in STRM_EXPORT_DIR).
  GET /api/strm/status           - Check STRM configuration and disk sync status.
"""

from __future__ import annotations

import os
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from Backend.fastapi.security.tokens import verify_token
from Backend.fastapi.security.credentials import require_auth
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.strm_generator import STRMExporter, resolve_strm_token
from Backend.logger import LOGGER

router = APIRouter(tags=["STRM & Emby"])


@router.get("/api/strm/status")
async def strm_status_api(_: bool = Depends(require_auth)):
    settings = SettingsManager.current()
    export_dir = settings.strm_export_dir
    dir_exists = os.path.isdir(export_dir) if export_dir else False
    strm_count = 0
    nfo_count = 0

    if dir_exists:
        try:
            for root, _, files in os.walk(export_dir):
                for f in files:
                    if f.endswith(".strm"):
                        strm_count += 1
                    elif f.endswith(".nfo"):
                        nfo_count += 1
        except Exception as e:
            LOGGER.warning(f"[STRM] Failed counting files in {export_dir}: {e}")

    return {
        "export_dir": export_dir,
        "dir_exists": dir_exists,
        "default_token_configured": bool(settings.strm_default_token),
        "strm_count": strm_count,
        "nfo_count": nfo_count,
    }


@router.post("/api/strm/sync")
async def strm_sync_api(_: bool = Depends(require_auth)):
    settings = SettingsManager.current()
    export_dir = settings.strm_export_dir
    if not export_dir:
        raise HTTPException(
            status_code=400,
            detail="STRM Export Directory is not configured. Set it in Settings -> Emby / STRM Support.",
        )

    token = await resolve_strm_token()
    exporter = STRMExporter(token=token, include_nfo=True)
    try:
        result = await exporter.export_to_directory(export_dir)
        return result
    except Exception as e:
        LOGGER.error(f"[STRM] Sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"STRM sync failed: {str(e)}")


@router.get("/api/strm/export.zip")
async def strm_export_zip_api(
    token: str = Query(None, description="API token to embed inside the .strm files"),
    include_nfo: bool = Query(True, description="Include companion .nfo metadata files"),
):
    resolved_token = await resolve_strm_token(token)
    exporter = STRMExporter(token=resolved_token, include_nfo=include_nfo)

    try:
        zip_buf = await exporter.create_zip_archive()
        headers = {
            "Content-Disposition": 'attachment; filename="telestream_emby_strm.zip"',
            "Content-Type": "application/zip",
        }
        return Response(content=zip_buf.getvalue(), media_type="application/zip", headers=headers)
    except Exception as e:
        LOGGER.error(f"[STRM] Zip export failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate STRM zip: {str(e)}")

import aiohttp
from fastapi.responses import HTMLResponse
from Backend.fastapi.routes.template_routes import templates


@router.get("/signup", response_class=HTMLResponse, tags=["Public Signup"])
async def public_signup_page(request: Request):
    """Public registration page for normal users to register for Emby."""
    settings = SettingsManager.current()
    if not settings.emby_public_signup:
        raise HTTPException(status_code=403, detail="Public user signup is currently disabled by administrator.")

    hostname = request.url.hostname or "localhost"
    emby_external = settings.emby_public_url or f"http://{hostname}:8096"
    return templates.TemplateResponse(
        "signup.html",
        {
            "request": request,
            "brand_name": "Telestream",
            "emby_external_url": emby_external,
        },
    )


@router.post("/api/signup", tags=["Public Signup"])
async def public_signup_api(payload: dict, request: Request):
    """API endpoint to create a restricted non-admin user in Emby."""
    settings = SettingsManager.current()
    if not settings.emby_public_signup:
        raise HTTPException(status_code=403, detail="Public user signup is currently disabled.")

    api_key = settings.emby_api_key
    if not api_key:
        raise HTTPException(status_code=500, detail="Emby API key is not configured in Telestream settings.")

    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "").strip()

    if not username:
        raise HTTPException(status_code=400, detail="Username is required.")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters.")

    emby_base = (settings.emby_url or "http://localhost:8096").rstrip("/")

    # 1. Create User in Emby via REST API: POST /Users/New
    async with aiohttp.ClientSession() as session:
        headers = {
            "X-Emby-Token": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        create_url = f"{emby_base}/Users/New"
        try:
            async with session.post(create_url, json={"Name": username}, headers=headers, timeout=10) as resp:
                if resp.status == 400:
                    err_txt = await resp.text()
                    raise HTTPException(status_code=400, detail=f"Username may already exist in Emby.")
                if resp.status not in (200, 204):
                    err_txt = await resp.text()
                    LOGGER.error(f"[Emby API] Create user failed {resp.status}: {err_txt}")
                    raise HTTPException(status_code=resp.status, detail="Failed creating user in Emby.")

                user_doc = await resp.json()
                user_id = user_doc.get("Id")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Emby server timed out.")
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            LOGGER.error(f"[Emby API] Connection failed: {e}")
            raise HTTPException(status_code=502, detail=f"Could not connect to Emby server: {e}")

        # 2. Set Password: POST /Users/{Id}/Password
        if user_id and password:
            pwd_url = f"{emby_base}/Users/{user_id}/Password"
            try:
                async with session.post(pwd_url, json={"NewPw": password, "ResetPassword": False}, headers=headers) as presp:
                    if presp.status not in (200, 204):
                        LOGGER.warning(f"[Emby API] Could not set user password: {presp.status}")
            except Exception as pe:
                LOGGER.warning(f"[Emby API] Password set failed: {pe}")

        # 3. Restrict permissions (Ensure non-admin): POST /Users/{Id}/Policy
        if user_id:
            policy_url = f"{emby_base}/Users/{user_id}/Policy"
            policy = {
                "IsAdministrator": False,
                "IsHidden": False,
                "EnableContentDownloading": False,
                "EnableUserPreferenceAccess": True,
            }
            try:
                async with session.post(policy_url, json=policy, headers=headers) as pol_resp:
                    pass
            except Exception as ple:
                LOGGER.warning(f"[Emby API] Policy update failed: {ple}")

    hostname = request.url.hostname or "localhost"
    login_url = settings.emby_public_url or f"http://{hostname}:8096"
    return {
        "status": "success",
        "username": username,
        "login_url": login_url,
    }
