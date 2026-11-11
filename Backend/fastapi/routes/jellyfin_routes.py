"""A deliberately small Jellyfin-compatible API for the Jellyfin Android client.

This is NOT the official Jellyfin Server. It exposes a subset of the Jellyfin HTTP
API over the existing Telegram-Stremio FastAPI application and reuses the existing
Telegram streaming pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict, deque
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from Backend import db
from Backend.config import Telegram
from Backend.fastapi.routes.stream_routes import stream_handler
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.strm_generator import resolve_strm_token
from Backend.logger import LOGGER

router = APIRouter(tags=["Jellyfin Compatibility"])

_ITEM_PREFIX = "tsjf_"
_ROOT_MOVIES_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "telegram-stremio:jellyfin:movies"))
_ROOT_SERIES_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "telegram-stremio:jellyfin:series"))
_INDEXES_READY = False
_CATALOG_CACHE: Optional[tuple[float, list[dict], list[dict]]] = None
_CATALOG_CACHE_TTL = 45
_CATALOG_LOCK = asyncio.Lock()
_AUTH_FAILURES: dict[str, deque[float]] = defaultdict(deque)
_AUTH_FAILURE_WINDOW_SECONDS = 300
_AUTH_FAILURE_LIMIT = 8


def _enabled() -> bool:
    return bool(
        getattr(Telegram, "JELLYFIN_COMPAT_ENABLED", False)
        and str(getattr(Telegram, "JELLYFIN_USERNAME", "") or "").strip()
        and str(getattr(Telegram, "JELLYFIN_PASSWORD", "") or "")
    )


def _require_enabled() -> None:
    if not _enabled():
        raise HTTPException(
            status_code=404,
            detail="Jellyfin compatibility is not enabled. Configure JELLYFIN_COMPAT_ENABLED, JELLYFIN_USERNAME and JELLYFIN_PASSWORD.",
        )


def _server_id() -> str:
    configured = str(getattr(Telegram, "JELLYFIN_SERVER_ID", "") or "").strip()
    if configured:
        return configured
    settings = SettingsManager.current()
    seed = f"{settings.base_url}|{settings.session_secret}|telegram-stremio-jellyfin"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _server_name() -> str:
    return str(getattr(Telegram, "JELLYFIN_SERVER_NAME", "Telegram-Stremio") or "Telegram-Stremio")


def _username() -> str:
    return str(getattr(Telegram, "JELLYFIN_USERNAME", "") or "").strip()


def _is_current_user(user_id: str) -> bool:
    return str(user_id).casefold() in {"me", _user_id().casefold()}


def _user_id() -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_server_id()}:{_username().casefold()}"))


def _user_dto() -> dict:
    return {
        "Name": _username(),
        "ServerId": _server_id(),
        "Id": _user_id(),
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "HasConfiguredEasyPassword": False,
        "EnableAutoLogin": False,
        "LastLoginDate": datetime.now(timezone.utc).isoformat(),
        "LastActivityDate": datetime.now(timezone.utc).isoformat(),
        "Policy": {
            "IsAdministrator": False,
            "IsHidden": False,
            "IsDisabled": False,
            "EnableRemoteAccess": True,
            "EnableLiveTvManagement": False,
            "EnableLiveTvAccess": False,
            "EnableMediaPlayback": True,
            "EnableAudioPlaybackTranscoding": False,
            "EnableVideoPlaybackTranscoding": False,
            "EnablePlaybackRemuxing": True,
            "EnableContentDeletion": False,
            "EnableContentDownloading": False,
        },
    }


def _parse_authorization_header(request: Request) -> dict:
    raw = request.headers.get("Authorization") or request.headers.get("X-Emby-Authorization") or ""
    values: dict[str, str] = {}
    match = re.search(r"(?:MediaBrowser|Emby)\s+(.*)$", raw, re.IGNORECASE)
    if match:
        for key, quoted_value, plain_value in re.findall(
            r'([A-Za-z][A-Za-z0-9]*)\s*=\s*(?:"([^"]*)"|([^,\s]+))', match.group(1)
        ):
            values[key.lower()] = quoted_value or plain_value
    token = (
        values.get("token")
        or request.headers.get("X-Emby-Token")
        or request.headers.get("X-MediaBrowser-Token")
        or request.query_params.get("api_key")
        or request.query_params.get("ApiKey")
    )
    return {
        "token": token,
        "client": values.get("client") or request.headers.get("X-Emby-Client", "Unknown client"),
        "device": values.get("device") or request.headers.get("X-Emby-Device-Name", "Unknown device"),
        "device_id": values.get("deviceid") or request.headers.get("X-Emby-Device-Id", "unknown-device"),
        "version": values.get("version") or request.headers.get("X-Emby-Client-Version", "unknown"),
    }


def _tracking_collection(name: str):
    tracking = db.dbs.get("tracking") if getattr(db, "dbs", None) else None
    if tracking is None:
        raise HTTPException(status_code=503, detail="Database is not ready")
    return tracking[name]


async def ensure_jellyfin_indexes() -> None:
    """Create compatibility collections/indexes after MongoDB is connected."""
    global _INDEXES_READY
    if _INDEXES_READY or not _enabled():
        return
    try:
        sessions = _tracking_collection("jellyfin_sessions")
        await sessions.create_index("token_hash", unique=True)
        await sessions.create_index("user_id")
        await sessions.create_index("device_id")
        await sessions.create_index("expires_at", expireAfterSeconds=0)
        await _tracking_collection("jellyfin_playback_state").create_index(
            [("user_id", 1), ("item_id", 1)], unique=True
        )
        _INDEXES_READY = True
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.warning("[JELLYFIN] Could not create indexes yet: %s", exc)


def _auth_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_auth_throttle(request: Request) -> str:
    ip = _auth_ip(request)
    now = time.monotonic()
    attempts = _AUTH_FAILURES[ip]
    while attempts and now - attempts[0] > _AUTH_FAILURE_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= _AUTH_FAILURE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many failed Jellyfin login attempts. Try again later.")
    return ip


def _record_auth_failure(ip: str) -> None:
    _AUTH_FAILURES[ip].append(time.monotonic())


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _expiry_is_past(value: Any) -> bool:
    if not value:
        return True
    now = datetime.now(timezone.utc) if getattr(value, "tzinfo", None) else datetime.utcnow()
    return value <= now


async def _session_for_request(request: Request) -> dict:
    _require_enabled()
    auth = _parse_authorization_header(request)
    token = auth.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Jellyfin authentication token is required")
    session = await _tracking_collection("jellyfin_sessions").find_one({"token_hash": _token_hash(token)})
    if not session or session.get("revoked_at") or _expiry_is_past(session.get("expires_at")):
        raise HTTPException(status_code=401, detail="Invalid or expired Jellyfin session")
    last_seen = session.get("last_seen_at")
    if not last_seen or (datetime.now(timezone.utc) - (last_seen.replace(tzinfo=timezone.utc) if last_seen.tzinfo is None else last_seen)).total_seconds() >= 60:
        await _tracking_collection("jellyfin_sessions").update_one(
            {"_id": session["_id"]},
            {"$set": {"last_seen_at": datetime.now(timezone.utc)}},
        )
    session["access_token"] = token
    return session


def _encode_item_id(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _ITEM_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_item_id(item_id: str) -> dict:
    if not item_id.startswith(_ITEM_PREFIX):
        raise HTTPException(status_code=404, detail="Unknown Jellyfin item")
    encoded = item_id[len(_ITEM_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=404, detail="Invalid Jellyfin item identifier")
    if not isinstance(payload, dict) or payload.get("collection") not in ("movie", "tv"):
        raise HTTPException(status_code=404, detail="Invalid Jellyfin item identifier")
    return payload


def _mongo_id(value: str):
    try:
        return ObjectId(value)
    except Exception:
        return value


def _storage_keys() -> list[str]:
    keys = [key for key in getattr(db, "dbs", {}).keys() if key.startswith("storage_")]
    return sorted(keys, key=lambda key: int(key.split("_", 1)[1]) if key.split("_", 1)[1].isdigit() else 0)


def _usable_quality(quality: dict) -> bool:
    return bool(quality and quality.get("id") and not quality.get("is_dead"))


def _best_quality(qualities: Optional[list[dict]]) -> Optional[dict]:
    order = {"2160p": 0, "4k": 0, "1440p": 1, "1080p": 2, "720p": 3, "480p": 4, "360p": 5}
    available = [item for item in (qualities or []) if _usable_quality(item)]
    if not available:
        return None
    return sorted(available, key=lambda item: order.get(str(item.get("quality") or "").lower(), 50))[0]


def _display_name(doc: dict) -> str:
    return str(doc.get("title_english") or doc.get("title") or doc.get("original_title") or "Untitled")


def _year(doc: dict) -> Optional[int]:
    try:
        return int(doc.get("release_year")) if doc.get("release_year") else None
    except (ValueError, TypeError):
        return None


def _runtime_ticks(value: Any) -> Optional[int]:
    """Convert the repository's common runtime formats to Jellyfin ticks.

    Metadata providers store runtimes as values such as ``"105"``,
    ``"105 min"`` or ``"1h 45m"``. Jellyfin expects 100-nanosecond ticks.
    """
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            minutes = float(text)
        else:
            hours_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", text)
            minutes_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b", text)
            if not hours_match and not minutes_match:
                return None
            minutes = (float(hours_match.group(1)) * 60 if hours_match else 0) + (float(minutes_match.group(1)) if minutes_match else 0)
        return int(minutes * 60 * 10_000_000) if minutes > 0 else None
    except (ValueError, TypeError):
        return None


def _image_tag(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return hashlib.md5(str(value).encode("utf-8")).hexdigest()


def _movie_payload(db_key: str, doc: dict) -> dict:
    return {"collection": "movie", "db": db_key, "object_id": str(doc.get("_id"))}


def _series_payload(db_key: str, doc: dict) -> dict:
    return {"collection": "tv", "db": db_key, "object_id": str(doc.get("_id"))}


def _season_payload(db_key: str, doc: dict, season_number: int) -> dict:
    return {"collection": "tv", "db": db_key, "object_id": str(doc.get("_id")), "season": int(season_number)}


def _episode_payload(db_key: str, doc: dict, season_number: int, episode_number: int) -> dict:
    return {
        "collection": "tv", "db": db_key, "object_id": str(doc.get("_id")),
        "season": int(season_number), "episode": int(episode_number),
    }


def _item_common(item_id: str, name: str, item_type: str, doc: dict) -> dict:
    poster = doc.get("poster")
    backdrop = doc.get("backdrop")
    result = {
        "Id": item_id,
        "Name": name,
        "ServerId": _server_id(),
        "Type": item_type,
        "IsFolder": item_type in ("Series", "Season"),
        "Overview": doc.get("description") or "",
        "Genres": doc.get("genres") or [],
        "ProductionYear": _year(doc),
        "CommunityRating": doc.get("rating"),
        "ProviderIds": {},
        "ImageTags": {},
        "BackdropImageTags": [],
        "DateCreated": (doc.get("updated_on") or datetime.now(timezone.utc)).isoformat()
            if hasattr(doc.get("updated_on") or datetime.now(timezone.utc), "isoformat") else None,
    }
    if doc.get("tmdb_id") is not None:
        result["ProviderIds"]["Tmdb"] = str(doc["tmdb_id"])
    if doc.get("imdb_id"):
        result["ProviderIds"]["Imdb"] = str(doc["imdb_id"])
    if doc.get("kitsu_id") is not None:
        result["ProviderIds"]["Kitsu"] = str(doc["kitsu_id"])
    if poster:
        result["ImageTags"]["Primary"] = _image_tag(poster)
    if backdrop:
        result["BackdropImageTags"] = [_image_tag(backdrop)]
    if not result["ImageTags"]:
        result.pop("ImageTags")
    if not result["BackdropImageTags"]:
        result.pop("BackdropImageTags")
    if not result["ProviderIds"]:
        result.pop("ProviderIds")
    return result


async def _load_top_level_items() -> tuple[list[dict], list[dict]]:
    """Read playable movie/show metadata from every configured storage database."""
    global _CATALOG_CACHE
    import time

    now = time.monotonic()
    if _CATALOG_CACHE and now - _CATALOG_CACHE[0] < _CATALOG_CACHE_TTL:
        return _CATALOG_CACHE[1], _CATALOG_CACHE[2]

    async with _CATALOG_LOCK:
        now = time.monotonic()
        if _CATALOG_CACHE and now - _CATALOG_CACHE[0] < _CATALOG_CACHE_TTL:
            return _CATALOG_CACHE[1], _CATALOG_CACHE[2]

        movies: list[dict] = []
        series: list[dict] = []
        keys = _storage_keys()
        if not keys:
            raise HTTPException(status_code=503, detail="No media storage database is connected")

        for db_key in keys:
            storage = db.dbs[db_key]
            movie_projection = {
                "title": 1, "title_english": 1, "original_title": 1, "tmdb_id": 1,
                "imdb_id": 1, "kitsu_id": 1, "genres": 1, "description": 1,
                "rating": 1, "release_year": 1, "poster": 1, "backdrop": 1,
                "runtime": 1, "updated_on": 1, "telegram.id": 1, "telegram.name": 1,
                "telegram.quality": 1, "telegram.size": 1, "telegram.is_dead": 1,
            }
            cursor = storage["movie"].find({}, movie_projection)
            async for doc in cursor:
                if not _best_quality(doc.get("telegram")):
                    continue
                item_id = _encode_item_id(_movie_payload(db_key, doc))
                item = _item_common(item_id, _display_name(doc), "Movie", doc)
                item["RunTimeTicks"] = _runtime_ticks(doc.get("runtime"))
                item["MediaType"] = "Video"
                movies.append(item)

            tv_projection = {
                "title": 1, "title_english": 1, "original_title": 1, "tmdb_id": 1,
                "imdb_id": 1, "kitsu_id": 1, "genres": 1, "description": 1,
                "rating": 1, "release_year": 1, "poster": 1, "backdrop": 1,
                "runtime": 1, "updated_on": 1, "seasons.season_number": 1,
                "seasons.episodes.episode_number": 1, "seasons.episodes.title": 1,
                "seasons.episodes.overview": 1, "seasons.episodes.released": 1,
                "seasons.episodes.episode_backdrop": 1, "seasons.episodes.telegram.id": 1,
                "seasons.episodes.telegram.name": 1, "seasons.episodes.telegram.quality": 1,
                "seasons.episodes.telegram.size": 1, "seasons.episodes.telegram.is_dead": 1,
            }
            cursor = storage["tv"].find({}, tv_projection)
            async for doc in cursor:
                playable_episode_count = sum(
                    1 for season in (doc.get("seasons") or [])
                    for episode in (season.get("episodes") or [])
                    if _best_quality(episode.get("telegram"))
                )
                if playable_episode_count <= 0:
                    continue
                item_id = _encode_item_id(_series_payload(db_key, doc))
                item = _item_common(item_id, _display_name(doc), "Series", doc)
                item["ChildCount"] = sum(1 for season in (doc.get("seasons") or []) if any(
                    _best_quality(ep.get("telegram")) for ep in (season.get("episodes") or [])
                ))
                item["RecursiveItemCount"] = playable_episode_count
                item["MediaType"] = "Video"
                series.append(item)

        movies.sort(key=lambda item: item.get("Name", "").casefold())
        series.sort(key=lambda item: item.get("Name", "").casefold())
        _CATALOG_CACHE = (time.monotonic(), movies, series)
        return movies, series


async def _get_payload_document(payload: dict) -> dict:
    db_key = payload.get("db")
    collection = payload.get("collection")
    object_id = payload.get("object_id")
    if db_key not in getattr(db, "dbs", {}) or not str(db_key).startswith("storage_"):
        raise HTTPException(status_code=404, detail="Media item no longer exists")
    if collection not in ("movie", "tv") or not object_id:
        raise HTTPException(status_code=404, detail="Invalid media item")
    doc = await db.dbs[db_key][collection].find_one({"_id": _mongo_id(str(object_id))})
    if not doc:
        raise HTTPException(status_code=404, detail="Media item no longer exists")
    return doc


async def _resolve_item(item_id: str) -> tuple[dict, dict, Optional[dict], Optional[dict]]:
    """Return (payload, parent document, season, episode) for a stable Jellyfin item id."""
    payload = _decode_item_id(item_id)
    doc = await _get_payload_document(payload)
    season_obj = None
    episode_obj = None
    if payload.get("season") is not None:
        season_obj = next((s for s in (doc.get("seasons") or []) if int(s.get("season_number", -999)) == int(payload["season"])), None)
        if not season_obj:
            raise HTTPException(status_code=404, detail="Season no longer exists")
    if payload.get("episode") is not None:
        episode_obj = next((e for e in (season_obj.get("episodes") or []) if int(e.get("episode_number", -999)) == int(payload["episode"])), None)
        if not episode_obj:
            raise HTTPException(status_code=404, detail="Episode no longer exists")
    return payload, doc, season_obj, episode_obj


def _season_item(db_key: str, doc: dict, season: dict) -> dict:
    sn = int(season.get("season_number") or 0)
    payload = _season_payload(db_key, doc, sn)
    item_id = _encode_item_id(payload)
    result = _item_common(item_id, f"Season {sn}", "Season", doc)
    result.update({
        "ParentId": _encode_item_id(_series_payload(db_key, doc)),
        "IndexNumber": sn,
        "ChildCount": sum(1 for ep in (season.get("episodes") or []) if _best_quality(ep.get("telegram"))),
    })
    return result


def _episode_item(db_key: str, doc: dict, season: dict, episode: dict) -> dict:
    sn = int(season.get("season_number") or 0)
    en = int(episode.get("episode_number") or 0)
    payload = _episode_payload(db_key, doc, sn, en)
    item_id = _encode_item_id(payload)
    name = str(episode.get("title") or f"Episode {en}")
    result = _item_common(item_id, name, "Episode", doc)
    backdrop = episode.get("episode_backdrop") or doc.get("backdrop")
    if backdrop:
        result["BackdropImageTags"] = [_image_tag(backdrop)]
    result.update({
        "ParentId": _encode_item_id(_season_payload(db_key, doc, sn)),
        "SeriesId": _encode_item_id(_series_payload(db_key, doc)),
        "SeriesName": _display_name(doc),
        "IndexNumber": en,
        "ParentIndexNumber": sn,
        "RunTimeTicks": _runtime_ticks(doc.get("runtime")),
        "MediaType": "Video",
    })
    if episode.get("overview"):
        result["Overview"] = episode["overview"]
    if episode.get("released"):
        result["PremiereDate"] = str(episode["released"])
    return result


async def _items_for_parent(parent_id: Optional[str], include_types: Optional[str], search_term: Optional[str], start: int, limit: int) -> dict:
    movies, series = await _load_top_level_items()
    allowed = {part.strip().lower() for part in (include_types or "").split(",") if part.strip()}
    results: list[dict] = []

    if not parent_id:
        if not allowed or "movie" in allowed:
            results.extend(movies)
        if not allowed or "series" in allowed:
            results.extend(series)
    elif parent_id in (_ROOT_MOVIES_ID, _ROOT_SERIES_ID):
        results = movies if parent_id == _ROOT_MOVIES_ID else series
    else:
        payload, doc, season_obj, episode_obj = await _resolve_item(parent_id)
        if payload.get("collection") == "tv" and payload.get("episode") is None:
            if season_obj is None:
                # A Series parent lists its playable seasons.
                for season in (doc.get("seasons") or []):
                    playable = [ep for ep in (season.get("episodes") or []) if _best_quality(ep.get("telegram"))]
                    if playable:
                        results.append(_season_item(payload["db"], doc, season))
            else:
                # A Season parent lists only playable episodes.
                for episode in (season_obj.get("episodes") or []):
                    if _best_quality(episode.get("telegram")):
                        results.append(_episode_item(payload["db"], doc, season_obj, episode))
    if search_term:
        needle = search_term.casefold()
        results = [item for item in results if needle in str(item.get("Name", "")).casefold()]
    if allowed and parent_id and parent_id not in (_ROOT_MOVIES_ID, _ROOT_SERIES_ID):
        results = [item for item in results if str(item.get("Type", "")).lower() in allowed]
    total = len(results)
    return {"Items": results[max(0, start):max(0, start) + max(1, min(limit, 200))], "TotalRecordCount": total, "StartIndex": max(0, start)}


def _view_item(view_id: str, name: str, collection_type: str, count: int) -> dict:
    return {
        "Name": name,
        "ServerId": _server_id(),
        "Id": view_id,
        "Etag": hashlib.md5(view_id.encode()).hexdigest(),
        "DateCreated": datetime.now(timezone.utc).isoformat(),
        "CanDelete": False,
        "CanDownload": False,
        "SortName": name,
        "Type": "CollectionFolder",
        "CollectionType": collection_type,
        "IsFolder": True,
        "ChildCount": count,
        "PresentationUniqueKey": view_id,
    }


def _image_url(doc: dict, season: Optional[dict], episode: Optional[dict], image_type: str) -> Optional[str]:
    image_type = image_type.lower()
    if image_type in ("primary", "thumb"):
        return doc.get("poster")
    if image_type in ("backdrop", "screenshot"):
        return (episode or {}).get("episode_backdrop") or doc.get("backdrop")
    if image_type == "logo":
        return doc.get("logo")
    return None


@router.get("/System/Ping")
async def jellyfin_ping():
    _require_enabled()
    return Response(status_code=200)


@router.get("/System/Info/Public")
async def jellyfin_public_info():
    _require_enabled()
    return {
        "LocalAddress": "",
        "ServerName": _server_name(),
        "Version": "10.10.7",
        "ProductName": "Jellyfin Server",
        "OperatingSystem": "Linux",
        "Id": _server_id(),
        "StartupWizardCompleted": True,
    }


@router.get("/System/Info")
async def jellyfin_system_info(request: Request):
    await _session_for_request(request)
    return {
        "ServerName": _server_name(),
        "Version": "10.10.7",
        "ProductName": "Jellyfin Server",
        "OperatingSystem": "Linux",
        "Id": _server_id(),
        "StartupWizardCompleted": True,
        "CanSelfRestart": False,
        "CanLaunchWebBrowser": False,
    }


@router.get("/Branding/Configuration")
async def jellyfin_branding_configuration():
    _require_enabled()
    return {"LoginDisclaimer": "", "CustomCss": "", "SplashscreenEnabled": False}


@router.get("/Users/Public")
async def jellyfin_public_users():
    _require_enabled()
    # The Jellyfin app can show this configured account and then prompt for its password.
    return [{
        "Name": _username(),
        "ServerId": _server_id(),
        "Id": _user_id(),
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "PrimaryImageTag": None,
    }]


@router.post("/Users/AuthenticateByName")
async def jellyfin_authenticate(request: Request):
    _require_enabled()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected a JSON login request")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object for login")
    ip = _check_auth_throttle(request)
    supplied_user = str(body.get("Username") or body.get("username") or "").strip()
    expected_user = _username()
    expected_password = str(getattr(Telegram, "JELLYFIN_PASSWORD", "") or "")

    user_ok = hmac.compare_digest(
        supplied_user.casefold().encode("utf-8"),
        expected_user.casefold().encode("utf-8"),
    )
    if "Pw" in body or "password" in body:
        supplied_password = str(body.get("Pw") if "Pw" in body else body.get("password") or "")
        password_ok = hmac.compare_digest(supplied_password.encode("utf-8"), expected_password.encode("utf-8"))
    elif body.get("Password"):
        # Jellyfin's request DTO also allows a SHA-1 password field for
        # compatibility with clients that send the pre-hashed value.
        supplied_hash = str(body.get("Password")).strip().lower()
        expected_hash = hashlib.sha1(expected_password.encode("utf-8")).hexdigest()
        password_ok = hmac.compare_digest(supplied_hash, expected_hash)
    else:
        password_ok = False

    if not (user_ok and password_ok):
        _record_auth_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _AUTH_FAILURES.pop(ip, None)

    await ensure_jellyfin_indexes()
    auth = _parse_authorization_header(request)
    access_token = secrets.token_urlsafe(48)
    ttl_days = max(1, min(int(getattr(Telegram, "JELLYFIN_TOKEN_TTL_DAYS", 30) or 30), 365))
    now = datetime.now(timezone.utc)
    session_doc = {
        "token_hash": _token_hash(access_token),
        "user_id": _user_id(),
        "username": expected_user,
        "client": auth["client"],
        "device": auth["device"],
        "device_id": auth["device_id"],
        "client_version": auth["version"],
        "created_at": now,
        "last_seen_at": now,
        "expires_at": now + timedelta(days=ttl_days),
        "revoked_at": None,
    }
    insert_result = await _tracking_collection("jellyfin_sessions").insert_one(session_doc)
    session_id = str(getattr(insert_result, "inserted_id", "") or "")
    user = _user_dto()
    return {
        "User": user,
        "SessionInfo": {
            "PlayState": {},
            "AdditionalUsers": [],
            "RemoteEndPoint": request.client.host if request.client else "",
            "UserId": _user_id(),
            "Id": session_id,
            "Client": auth["client"],
            "DeviceName": auth["device"],
            "DeviceId": auth["device_id"],
            "ApplicationVersion": auth["version"],
            "ServerId": _server_id(),
        },
        "AccessToken": access_token,
        "ServerId": _server_id(),
    }


@router.get("/Users/Me")
async def jellyfin_me(request: Request):
    await _session_for_request(request)
    return _user_dto()


@router.get("/Users/{user_id}")
async def jellyfin_get_user(user_id: str, request: Request):
    await _session_for_request(request)
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return _user_dto()


@router.post("/Sessions/Logout")
async def jellyfin_logout(request: Request):
    session = await _session_for_request(request)
    await _tracking_collection("jellyfin_sessions").delete_one({"token_hash": _token_hash(session["access_token"])})
    return Response(status_code=204)


@router.get("/UserViews")
async def jellyfin_user_views(request: Request, user_id: Optional[str] = Query(None, alias="UserId")):
    await _session_for_request(request)
    movies, series = await _load_top_level_items()
    return {"Items": [
        _view_item(_ROOT_MOVIES_ID, "Movies", "movies", len(movies)),
        _view_item(_ROOT_SERIES_ID, "TV Shows", "tvshows", len(series)),
    ], "TotalRecordCount": 2}


@router.get("/Users/{user_id}/Views")
async def jellyfin_user_views_by_id(user_id: str, request: Request):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return await jellyfin_user_views(request, user_id)


async def _items_response(request: Request, parent_id: Optional[str], include_item_types: Optional[str], search_term: Optional[str], start_index: int, limit: int):
    await _session_for_request(request)
    return await _items_for_parent(parent_id, include_item_types, search_term, start_index, limit)


@router.get("/Items")
async def jellyfin_items(
    request: Request,
    parent_id: Optional[str] = Query(None, alias="ParentId"),
    include_item_types: Optional[str] = Query(None, alias="IncludeItemTypes"),
    search_term: Optional[str] = Query(None, alias="SearchTerm"),
    start_index: int = Query(0, alias="StartIndex", ge=0),
    limit: int = Query(100, alias="Limit", ge=1, le=200),
    recursive: bool = Query(True, alias="Recursive"),
):
    return await _items_response(request, parent_id, include_item_types, search_term, start_index, limit)


@router.get("/Users/{user_id}/Items")
async def jellyfin_user_items(
    user_id: str,
    request: Request,
    parent_id: Optional[str] = Query(None, alias="ParentId"),
    include_item_types: Optional[str] = Query(None, alias="IncludeItemTypes"),
    search_term: Optional[str] = Query(None, alias="SearchTerm"),
    start_index: int = Query(0, alias="StartIndex", ge=0),
    limit: int = Query(100, alias="Limit", ge=1, le=200),
    recursive: bool = Query(True, alias="Recursive"),
):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return await _items_response(request, parent_id, include_item_types, search_term, start_index, limit)


@router.get("/Users/{user_id}/Items/Latest")
async def jellyfin_latest_items(
    user_id: str, request: Request,
    limit: int = Query(20, alias="Limit", ge=1, le=100),
):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    await _session_for_request(request)
    movies, series = await _load_top_level_items()
    return sorted(movies + series, key=lambda item: item.get("DateCreated", ""), reverse=True)[:limit]


@router.get("/Users/{user_id}/Items/Resume")
async def jellyfin_resume_items(
    user_id: str, request: Request,
    limit: int = Query(20, alias="Limit", ge=1, le=100),
):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    session = await _session_for_request(request)
    collection = _tracking_collection("jellyfin_playback_state")
    cursor = collection.find({"user_id": session["user_id"], "played": False}).sort("updated_at", -1).limit(limit)
    states = await cursor.to_list(length=limit)
    results = []
    for state in states:
        try:
            payload, doc, season_obj, episode_obj = await _resolve_item(str(state.get("item_id") or ""))
            if payload.get("collection") == "movie":
                item = _item_common(str(state["item_id"]), _display_name(doc), "Movie", doc)
            elif episode_obj:
                item = _episode_item(payload["db"], doc, season_obj, episode_obj)
            else:
                continue
            item["UserData"] = {
                "PlaybackPositionTicks": int(state.get("position_ticks") or 0),
                "Played": False,
                "PlayCount": 0,
                "IsFavorite": False,
            }
            results.append(item)
        except HTTPException:
            continue
    return {"Items": results, "TotalRecordCount": len(results), "StartIndex": 0}


@router.get("/Users/{user_id}/Items/{item_id}")
async def jellyfin_user_item(user_id: str, item_id: str, request: Request):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return await jellyfin_item(item_id, request)


@router.get("/Items/{item_id}")
async def jellyfin_item(item_id: str, request: Request):
    await _session_for_request(request)
    payload, doc, season, episode = await _resolve_item(item_id)
    if payload.get("collection") == "movie":
        result = _item_common(item_id, _display_name(doc), "Movie", doc)
        result["RunTimeTicks"] = _runtime_ticks(doc.get("runtime"))
        return result
    if payload.get("episode") is not None and season and episode:
        return _episode_item(payload["db"], doc, season, episode)
    if payload.get("season") is not None and season:
        return _season_item(payload["db"], doc, season)
    movies, series = await _load_top_level_items()
    return next((item for item in series if item["Id"] == item_id), _item_common(item_id, _display_name(doc), "Series", doc))




@router.get("/Shows/{series_id}/Seasons")
async def jellyfin_show_seasons(series_id: str, request: Request):
    await _session_for_request(request)
    payload, doc, _, _ = await _resolve_item(series_id)
    if payload.get("collection") != "tv" or payload.get("season") is not None:
        raise HTTPException(status_code=400, detail="Item is not a series")
    seasons = [_season_item(payload["db"], doc, season) for season in (doc.get("seasons") or []) if any(
        _best_quality(ep.get("telegram")) for ep in (season.get("episodes") or [])
    )]
    return {"Items": seasons, "TotalRecordCount": len(seasons), "StartIndex": 0}


@router.get("/Shows/{series_id}/Episodes")
async def jellyfin_show_episodes(
    series_id: str,
    request: Request,
    season: Optional[int] = Query(None, alias="Season"),
    season_id: Optional[str] = Query(None, alias="SeasonId"),
):
    await _session_for_request(request)
    payload, doc, _, _ = await _resolve_item(series_id)
    if payload.get("collection") != "tv":
        raise HTTPException(status_code=400, detail="Item is not a series")
    chosen_seasons = [s for s in (doc.get("seasons") or []) if season is None or int(s.get("season_number", -999)) == season]
    if season_id:
        season_payload, _, season_doc, _ = await _resolve_item(season_id)
        if season_payload.get("object_id") != payload.get("object_id") or not season_doc:
            raise HTTPException(status_code=400, detail="Season does not belong to series")
        chosen_seasons = [season_doc]
    episodes = []
    for season_doc in chosen_seasons:
        episodes.extend(_episode_item(payload["db"], doc, season_doc, episode)
                         for episode in (season_doc.get("episodes") or [])
                         if _best_quality(episode.get("telegram")))
    return {"Items": episodes, "TotalRecordCount": len(episodes), "StartIndex": 0}


@router.get("/Items/{item_id}/PlaybackInfo")
@router.post("/Items/{item_id}/PlaybackInfo")
async def jellyfin_playback_info(item_id: str, request: Request):
    session = await _session_for_request(request)
    payload, doc, season, episode = await _resolve_item(item_id)
    if payload.get("collection") == "movie":
        quality = _best_quality(doc.get("telegram"))
    elif episode:
        quality = _best_quality(episode.get("telegram"))
    else:
        raise HTTPException(status_code=400, detail="Only movies and episodes can be played")
    if not quality:
        raise HTTPException(status_code=404, detail="No playable Telegram quality is available")

    filename = str(quality.get("name") or f"{_display_name(doc)}.mkv")
    container = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mkv"
    # Jellyfin clients commonly append/send their access token on media URLs.
    # Include this Jellyfin session token so native players that do not reuse
    # the API Authorization header can still open the stream. The Telegram
    # source token is never exposed to the client.
    stream_url = (
        str(request.base_url).rstrip("/")
        + f"/Videos/{quote(item_id, safe='')}/stream?api_key={quote(session['access_token'], safe='')}"
    )
    return {
        "PlaySessionId": secrets.token_hex(16),
        "ItemId": item_id,
        "MediaSources": [{
            "Protocol": "Http",
            "Id": hashlib.sha256(f"{item_id}:{quality.get('id')}".encode()).hexdigest()[:32],
            "Path": stream_url,
            "Name": filename,
            "Container": container,
            "Type": "Default",
            "IsRemote": False,
            "SupportsDirectPlay": True,
            "SupportsDirectStream": True,
            "SupportsTranscoding": False,
            "RequiresOpening": False,
            "RequiresClosing": False,
            "ReadAtNativeFramerate": True,
            "DirectStreamUrl": stream_url,
            "MediaStreams": [],
            "Bitrate": None,
            "Size": None,
        }],
        "ErrorCode": None,
        "SessionId": str(session.get("_id") or ""),
    }


@router.get("/Videos/{item_id}/stream")
@router.head("/Videos/{item_id}/stream")
async def jellyfin_video_stream(item_id: str, request: Request):
    session = await _session_for_request(request)
    payload, doc, season, episode = await _resolve_item(item_id)
    if payload.get("collection") == "movie":
        quality = _best_quality(doc.get("telegram"))
    elif episode:
        quality = _best_quality(episode.get("telegram"))
    else:
        raise HTTPException(status_code=400, detail="Only movies and episodes can be streamed")
    if not quality or not quality.get("id"):
        raise HTTPException(status_code=404, detail="No playable Telegram file is available")

    source_token = await resolve_strm_token()
    if not source_token or source_token == "public":
        raise HTTPException(
            status_code=503,
            detail="Telegram-Stremio needs an active API token for playback. Set a STRM default token in the Web Panel or create an API token.",
        )
    source_token_data = await verify_token(source_token)
    if source_token_data.get("subscription_expired"):
        raise HTTPException(status_code=403, detail="The configured Telegram streaming token has expired")
    if source_token_data.get("limit_exceeded"):
        raise HTTPException(status_code=403, detail="The configured Telegram streaming token has reached its data limit")

    # Reuse the existing /dl implementation so split files, Telegram client
    # selection, byte ranges, analytics and file headers keep their existing behavior.
    filename = str(quality.get("name") or f"{_display_name(doc)}.mkv")
    # The existing streamer reads the filename from the /dl route's path
    # parameters for content-disposition and stream-title metadata. Because
    # this route dispatches internally, preserve that parameter explicitly.
    request.path_params["name"] = filename
    request.path_params["id"] = str(quality["id"])
    return await stream_handler(
        request=request,
        token=source_token,
        id=str(quality["id"]),
        name=filename,
        token_data=source_token_data,
    )


@router.get("/Items/{item_id}/Images/{image_type}")
@router.get("/Items/{item_id}/Images/{image_type}/{image_index}")
async def jellyfin_item_image(item_id: str, image_type: str, request: Request, image_index: int = 0):
    await _session_for_request(request)
    payload, doc, season, episode = await _resolve_item(item_id)
    image_url = _image_url(doc, season, episode, image_type)
    if not image_url:
        raise HTTPException(status_code=404, detail="Image not found")
    if str(image_url).startswith("/"):
        image_url = "https://image.tmdb.org/t/p/original" + str(image_url)
    return RedirectResponse(url=str(image_url), status_code=307)


@router.post("/Sessions/Playing")
async def jellyfin_playing(request: Request):
    await _session_for_request(request)
    return Response(status_code=204)


@router.post("/Sessions/Playing/Progress")
async def jellyfin_playing_progress(request: Request):
    session = await _session_for_request(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    item_id = str(body.get("ItemId") or "")
    if item_id:
        await _tracking_collection("jellyfin_playback_state").update_one(
            {"user_id": session["user_id"], "item_id": item_id},
            {"$set": {
                "user_id": session["user_id"],
                "item_id": item_id,
                "position_ticks": int(body.get("PlaybackPositionTicks") or 0),
                "played": False,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    return Response(status_code=204)


@router.post("/Sessions/Playing/Stopped")
async def jellyfin_playing_stopped(request: Request):
    session = await _session_for_request(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    item_id = str(body.get("ItemId") or "")
    if item_id:
        await _tracking_collection("jellyfin_playback_state").update_one(
            {"user_id": session["user_id"], "item_id": item_id},
            {"$set": {
                "user_id": session["user_id"],
                "item_id": item_id,
                "position_ticks": int(body.get("PlaybackPositionTicks") or 0),
                "played": bool(body.get("Played", False) or body.get("PlayedToCompletion", False)),
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    return Response(status_code=204)


# Jellyfin clients and SDKs may request lowercase paths (for example,
# /system/info/public). Jellyfin's own server routing is case-insensitive,
# but FastAPI/Starlette is case-sensitive. Register lowercase aliases for all
# compatibility endpoints so the Android client can use either form.
from fastapi.routing import APIRoute as _APIRoute

for _original_route in tuple(router.routes):
    if isinstance(_original_route, _APIRoute) and _original_route.path != _original_route.path.lower():
        router.add_api_route(
            _original_route.path.lower(),
            _original_route.endpoint,
            methods=sorted(_original_route.methods or []),
            name=f"{_original_route.name}_lowercase",
            include_in_schema=False,
        )
