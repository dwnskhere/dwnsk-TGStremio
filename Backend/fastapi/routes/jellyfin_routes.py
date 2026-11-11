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
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response

from Backend import db
from Backend.config import Telegram
from Backend.fastapi.routes.stream_routes import stream_handler, subtitle_handler
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.subtitles import get_subtitles_for, subtitle_ext
from Backend.logger import LOGGER

router = APIRouter(tags=["Jellyfin Compatibility"])

_ITEM_PREFIX = "tsjf_"  # Legacy IDs remain readable for existing clients/sessions.
_ITEM_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "telegram-stremio:jellyfin:item-ids:v1")
_ITEM_PAYLOAD_CACHE: dict[str, dict] = {}
_ITEM_PAYLOAD_CACHE_LIMIT = 100_000
_ROOT_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "telegram-stremio:jellyfin:root"))
_ROOT_MOVIES_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "telegram-stremio:jellyfin:movies"))
_ROOT_SERIES_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "telegram-stremio:jellyfin:series"))
_INDEXES_READY = False
_CATALOG_CACHE: Optional[tuple[float, str, list[dict], list[dict]]] = None
_CATALOG_CACHE_TTL = 45
_CATALOG_LOCK = asyncio.Lock()
_SOURCE_ACCESS_CACHE: Optional[tuple[float, str, dict]] = None
_SOURCE_ACCESS_CACHE_TTL = 15
_AUTH_FAILURES: dict[str, deque[float]] = defaultdict(deque)
_AUTH_FAILURE_WINDOW_SECONDS = 300
_AUTH_FAILURE_LIMIT = 8
_JELLYFIN_API_PREFIXES = (
    "/system/", "/users", "/sessions", "/items", "/userviews",
    "/displaypreferences", "/shows/", "/videos/", "/useritems/",
    "/userfavoriteitems", "/search/", "/genres", "/years", "/studios",
    "/persons", "/quickconnect/", "/auth/", "/branding/", "/playback/",
    "/livetv/", "/jellyfin/", "/web/",
)


def is_jellyfin_client_path(path: str) -> bool:
    """Return whether a path belongs to the Jellyfin API/Web client surface."""
    normalized = (path or "").casefold()
    return normalized.startswith(_JELLYFIN_API_PREFIXES)


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


async def _source_access() -> tuple[str, dict]:
    """Resolve and validate the Telegram-Stremio API token backing this Jellyfin view.

    A Jellyfin login is deliberately separate from the source API token. The
    latter controls visibility/subscription access and is never returned to
    the Jellyfin client.
    """
    global _SOURCE_ACCESS_CACHE
    configured = str(getattr(Telegram, "JELLYFIN_SOURCE_TOKEN", "") or "").strip()
    if not configured:
        configured = str(getattr(SettingsManager.current(), "strm_default_token", "") or "").strip()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "Jellyfin compatibility needs a Telegram-Stremio source API token. "
                "Set JELLYFIN_SOURCE_TOKEN in Heroku or configure the STRM default token."
            ),
        )
    signature = hashlib.sha256(configured.encode("utf-8")).hexdigest()
    now = time.monotonic()
    if _SOURCE_ACCESS_CACHE and _SOURCE_ACCESS_CACHE[1] == signature and now - _SOURCE_ACCESS_CACHE[0] < _SOURCE_ACCESS_CACHE_TTL:
        return configured, dict(_SOURCE_ACCESS_CACHE[2])

    source_data = await verify_token(configured)
    if not isinstance(source_data, dict):
        raise HTTPException(status_code=503, detail="The configured Telegram-Stremio source token could not be validated")
    if source_data.get("subscription_expired"):
        raise HTTPException(status_code=403, detail="The configured Telegram-Stremio source token has expired")
    if source_data.get("limit_exceeded"):
        raise HTTPException(status_code=403, detail="The configured Telegram-Stremio source token has reached its data limit")
    source_data["token"] = configured
    _SOURCE_ACCESS_CACHE = (now, signature, dict(source_data))
    return configured, source_data


def _source_token_can_view(mode: str, allowed_tokens: list, token_data: Optional[dict]) -> bool:
    """Keep Jellyfin listings consistent with Telegram-Stremio visibility rules."""
    if not token_data:
        return mode == "public"
    user_id = token_data.get("user_id")
    try:
        if user_id is not None and int(user_id) == int(Telegram.OWNER_ID):
            return True
    except (TypeError, ValueError):
        pass
    if mode == "owner":
        return False
    if mode == "tokens":
        return token_data.get("token") in (allowed_tokens or [])
    if SettingsManager.current().subscription:
        return not token_data.get("subscription_expired")
    return True


def _source_visibility_query(token_data: Optional[dict]) -> dict:
    if not token_data:
        return {"$or": [{"visibility": {"$exists": False}}, {"visibility": "public"}]}
    user_id = token_data.get("user_id")
    try:
        if user_id is not None and int(user_id) == int(Telegram.OWNER_ID):
            return {}
    except (TypeError, ValueError):
        pass
    return {"$or": [
        {"visibility": {"$exists": False}},
        {"visibility": "public"},
        {"visibility": "tokens", "allowed_tokens": token_data.get("token")},
    ]}


def _source_exclusive_query() -> dict:
    # Jellyfin exposes one global library, not individual Stremio catalogs.
    # Do not leak titles attached to a catalog-only exclusive collection.
    return {"$or": [
        {"exclusive_catalog_id": {"$exists": False}},
        {"exclusive_catalog_id": None},
    ]}


def _source_item_allowed(doc: dict, token_data: dict) -> bool:
    if not _source_token_can_view(
        doc.get("visibility") or "public",
        doc.get("allowed_tokens") or [],
        token_data,
    ):
        return False
    if doc.get("exclusive_catalog_id"):
        return False
    return True


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
        tickets = _tracking_collection("jellyfin_playback_tickets")
        await tickets.create_index("token_hash", unique=True)
        await tickets.create_index("session_id")
        await tickets.create_index("expires_at", expireAfterSeconds=0)
        await _tracking_collection("jellyfin_playback_state").create_index(
            [("user_id", 1), ("item_id", 1)], unique=True
        )
        await _tracking_collection("jellyfin_playback_state").create_index(
            [("user_id", 1), ("played", 1), ("updated_at", -1)]
        )
        await _tracking_collection("jellyfin_display_preferences").create_index(
            [("user_id", 1), ("preference_id", 1), ("client", 1)], unique=True
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
    if isinstance(value, str):
        text = value.strip()
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return True
    if not isinstance(value, datetime):
        return True
    now = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= now


async def _session_from_token(token: Optional[str]) -> dict:
    _require_enabled()
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


async def _session_for_request(request: Request) -> dict:
    auth = _parse_authorization_header(request)
    return await _session_from_token(auth.get("token"))


async def _create_playback_ticket(
    *, session: dict, item_id: str, purpose: str, track_index: Optional[int] = None,
) -> str:
    """Create a short-lived, item-scoped URL credential for native players.

    Jellyfin clients often open media URLs outside the API request pipeline and
    put the credential in the query string. Never put the long-lived session
    token in those URLs: query strings can be retained by proxies and logs.
    """
    await ensure_jellyfin_indexes()
    raw_ticket = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    ttl_seconds = max(300, min(int(getattr(Telegram, "JELLYFIN_PLAYBACK_TICKET_TTL_SECONDS", 21600) or 21600), 86400))
    await _tracking_collection("jellyfin_playback_tickets").insert_one({
        "token_hash": _token_hash(raw_ticket),
        "session_id": session["_id"],
        "user_id": session["user_id"],
        "item_id": str(item_id),
        "purpose": str(purpose),
        "track_index": track_index,
        "created_at": now,
        "expires_at": now + timedelta(seconds=ttl_seconds),
    })
    return raw_ticket


async def _session_for_media_request(
    request: Request, *, item_id: str, purpose: str, track_index: Optional[int] = None,
) -> dict:
    """Validate either a normal session token or a scoped playback ticket."""
    auth = _parse_authorization_header(request)
    token = auth.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Jellyfin playback authorization is required")

    # Authorization headers can carry a full session token. Query-string
    # api_key values emitted by PlaybackInfo are short-lived playback tickets.
    query_ticket = request.query_params.get("api_key") or request.query_params.get("ApiKey")
    if token and query_ticket != token:
        return await _session_from_token(token)

    ticket = await _tracking_collection("jellyfin_playback_tickets").find_one({
        "token_hash": _token_hash(token),
        "item_id": str(item_id),
        "purpose": str(purpose),
        "track_index": track_index,
    })
    if not ticket or _expiry_is_past(ticket.get("expires_at")):
        raise HTTPException(status_code=401, detail="Invalid or expired Jellyfin playback ticket")

    session = await _tracking_collection("jellyfin_sessions").find_one({"_id": ticket.get("session_id")})
    if not session or session.get("revoked_at") or _expiry_is_past(session.get("expires_at")):
        raise HTTPException(status_code=401, detail="The Jellyfin session for this playback ticket is no longer valid")
    # Sliding expiry keeps a long movie/episode alive while active playback
    # continues, but an abandoned URL still expires after the configured idle
    # window. Ticket access never renews the user's main session token.
    ttl_seconds = max(300, min(int(getattr(Telegram, "JELLYFIN_PLAYBACK_TICKET_TTL_SECONDS", 21600) or 21600), 86400))
    await _tracking_collection("jellyfin_playback_tickets").update_one(
        {"_id": ticket["_id"]},
        {"$set": {
            "last_used_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        }},
    )
    session["access_token"] = None
    return session


def _encode_item_id(payload: dict) -> str:
    """Return a deterministic UUID, as expected by the Jellyfin item API.

    The corresponding opaque source reference is kept server-side only. UUID5
    makes IDs stable across Heroku dyno restarts without exposing Mongo IDs or
    database names to the Jellyfin client.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    item_id = str(uuid.uuid5(_ITEM_ID_NAMESPACE, raw.decode("utf-8")))
    _ITEM_PAYLOAD_CACHE[item_id] = dict(payload)
    if len(_ITEM_PAYLOAD_CACHE) > _ITEM_PAYLOAD_CACHE_LIMIT:
        # A bounded process cache; IDs can always be reconstructed from the
        # persistent Telegram catalogue after a cache eviction or dyno restart.
        _ITEM_PAYLOAD_CACHE.pop(next(iter(_ITEM_PAYLOAD_CACHE)))
    return item_id


def _decode_item_id(item_id: str) -> dict:
    # Backward compatibility for the short-lived pre-v3 build which emitted
    # base64-prefixed IDs. New IDs are UUIDs and resolve through our private
    # payload cache, rebuilt from MongoDB after dyno restarts.
    if item_id.startswith(_ITEM_PREFIX):
        encoded = item_id[len(_ITEM_PREFIX):]
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=404, detail="Invalid Jellyfin item identifier")
        if not isinstance(payload, dict) or payload.get("collection") not in ("movie", "tv"):
            raise HTTPException(status_code=404, detail="Invalid Jellyfin item identifier")
        return payload
    try:
        canonical_id = str(uuid.UUID(str(item_id)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="Unknown Jellyfin item")
    payload = _ITEM_PAYLOAD_CACHE.get(canonical_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Unknown Jellyfin item")
    return dict(payload)


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


def _date_iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return None
    text = str(value).strip()
    return text or None


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


def _metadata_list(value: Any) -> list[str]:
    """Normalize metadata that may be stored as a string or a list in MongoDB."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        normalized = []
        for item in value:
            if isinstance(item, str):
                candidate = item.strip()
            elif isinstance(item, dict):
                candidate = str(item.get("name") or item.get("Name") or item.get("title") or "").strip()
            else:
                candidate = str(item).strip() if item is not None else ""
            if candidate:
                normalized.append(candidate)
        return normalized
    return [str(value).strip()] if str(value).strip() else []


def _item_common(item_id: str, name: str, item_type: str, doc: dict) -> dict:
    poster = doc.get("poster")
    backdrop = doc.get("backdrop")
    result = {
        "Id": item_id,
        "Name": name,
        "SortName": str(name).casefold(),
        "ServerId": _server_id(),
        "Type": item_type,
        "IsFolder": item_type in ("Series", "Season"),
        "Overview": doc.get("description") or "",
        "Genres": _metadata_list(doc.get("genres")),
        "Studios": _metadata_list(doc.get("studios") or doc.get("production_companies")),
        "ProductionYear": _year(doc),
        "OriginalTitle": doc.get("original_title") or doc.get("title") or name,
        "CommunityRating": doc.get("rating"),
        "ProviderIds": {},
        "ImageTags": {},
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
        },
        "CanDownload": False,
        "CanDelete": False,
        "LocationType": "Virtual",
        "MediaSourceCount": 1,
        "DateCreated": _date_iso(doc.get("updated_on") or doc.get("created_at")),
    }
    if doc.get("tmdb_id") is not None:
        result["ProviderIds"]["Tmdb"] = str(doc["tmdb_id"])
    if doc.get("imdb_id"):
        result["ProviderIds"]["Imdb"] = str(doc["imdb_id"])
    if doc.get("kitsu_id") is not None:
        result["ProviderIds"]["Kitsu"] = str(doc["kitsu_id"])
    cast = doc.get("cast") or []
    people = []
    for person in cast:
        if isinstance(person, str):
            person_name, role, person_type = person, "", "Actor"
        elif isinstance(person, dict):
            person_name = str(person.get("name") or person.get("Name") or "").strip()
            role = str(person.get("character") or person.get("role") or person.get("Role") or "")
            person_type = str(person.get("type") or "Actor")
        else:
            continue
        if person_name:
            people.append({"Name": person_name, "Role": role, "Type": person_type})
    if people:
        result["People"] = people
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
    _, source_data = await _source_access()
    signature = hashlib.sha256(str(source_data.get("token") or "").encode("utf-8")).hexdigest()
    now = time.monotonic()
    if _CATALOG_CACHE and _CATALOG_CACHE[1] == signature and now - _CATALOG_CACHE[0] < _CATALOG_CACHE_TTL:
        return _CATALOG_CACHE[2], _CATALOG_CACHE[3]

    async with _CATALOG_LOCK:
        now = time.monotonic()
        if _CATALOG_CACHE and _CATALOG_CACHE[1] == signature and now - _CATALOG_CACHE[0] < _CATALOG_CACHE_TTL:
            return _CATALOG_CACHE[2], _CATALOG_CACHE[3]

        movies: list[dict] = []
        series: list[dict] = []
        # Match Stremio's precedence: the newest storage database wins when
        # the same TMDB title has been copied across storage databases.
        keys = list(reversed(_storage_keys()))
        if not keys:
            raise HTTPException(status_code=503, detail="No media storage database is connected")

        movie_seen: set[str] = set()
        series_seen: set[str] = set()
        visibility_filter = _source_visibility_query(source_data)
        exclusive_filter = _source_exclusive_query()
        query = {"$and": [visibility_filter, exclusive_filter]} if visibility_filter and exclusive_filter else (visibility_filter or exclusive_filter)

        for db_key in keys:
            storage = db.dbs[db_key]
            movie_projection = {
                "title": 1, "title_english": 1, "original_title": 1, "tmdb_id": 1,
                "imdb_id": 1, "kitsu_id": 1, "genres": 1, "description": 1, "cast": 1,
                "studios": 1, "production_companies": 1, "original_title": 1,
                "rating": 1, "release_year": 1, "poster": 1, "backdrop": 1,
                "runtime": 1, "updated_on": 1, "telegram.id": 1, "telegram.name": 1,
                "telegram.quality": 1, "telegram.size": 1, "telegram.is_dead": 1,
                "visibility": 1, "allowed_tokens": 1, "exclusive_catalog_id": 1,
                "exclusive_searchable": 1,
            }
            cursor = storage["movie"].find(query, movie_projection)
            async for doc in cursor:
                if not _source_item_allowed(doc, source_data):
                    continue
                if not _best_quality(doc.get("telegram")):
                    continue
                dedupe_key = f"tmdb:{doc.get('tmdb_id')}" if doc.get("tmdb_id") is not None else f"mongo:{db_key}:{doc.get('_id')}"
                if dedupe_key in movie_seen:
                    continue
                movie_seen.add(dedupe_key)
                item_id = _encode_item_id(_movie_payload(db_key, doc))
                item = _item_common(item_id, _display_name(doc), "Movie", doc)
                item["RunTimeTicks"] = _runtime_ticks(doc.get("runtime"))
                item["MediaType"] = "Video"
                movies.append(item)

            tv_projection = {
                "title": 1, "title_english": 1, "original_title": 1, "tmdb_id": 1,
                "imdb_id": 1, "kitsu_id": 1, "genres": 1, "description": 1, "cast": 1,
                "studios": 1, "production_companies": 1, "original_title": 1,
                "rating": 1, "release_year": 1, "poster": 1, "backdrop": 1,
                "runtime": 1, "updated_on": 1, "seasons.season_number": 1,
                "seasons.episodes.episode_number": 1, "seasons.episodes.title": 1,
                "seasons.episodes.overview": 1, "seasons.episodes.released": 1,
                "seasons.episodes.episode_backdrop": 1, "seasons.episodes.telegram.id": 1,
                "seasons.episodes.telegram.name": 1, "seasons.episodes.telegram.quality": 1,
                "seasons.episodes.telegram.size": 1, "seasons.episodes.telegram.is_dead": 1,
                "visibility": 1, "allowed_tokens": 1, "exclusive_catalog_id": 1,
                "exclusive_searchable": 1,
            }
            cursor = storage["tv"].find(query, tv_projection)
            async for doc in cursor:
                if not _source_item_allowed(doc, source_data):
                    continue
                playable_episode_count = sum(
                    1 for season in (doc.get("seasons") or [])
                    for episode in (season.get("episodes") or [])
                    if _best_quality(episode.get("telegram"))
                )
                if playable_episode_count <= 0:
                    continue
                dedupe_key = f"tmdb:{doc.get('tmdb_id')}" if doc.get("tmdb_id") is not None else f"mongo:{db_key}:{doc.get('_id')}"
                if dedupe_key in series_seen:
                    continue
                series_seen.add(dedupe_key)
                item_id = _encode_item_id(_series_payload(db_key, doc))
                for season_doc in (doc.get("seasons") or []):
                    season_number = int(season_doc.get("season_number") or 0)
                    playable_episodes = [
                        episode_doc for episode_doc in (season_doc.get("episodes") or [])
                        if _best_quality(episode_doc.get("telegram"))
                    ]
                    if playable_episodes:
                        _encode_item_id(_season_payload(db_key, doc, season_number))
                        for episode_doc in playable_episodes:
                            _encode_item_id(_episode_payload(
                                db_key, doc, season_number, int(episode_doc.get("episode_number") or 0)
                            ))
                item = _item_common(item_id, _display_name(doc), "Series", doc)
                item["ChildCount"] = sum(1 for season in (doc.get("seasons") or []) if any(
                    _best_quality(ep.get("telegram")) for ep in (season.get("episodes") or [])
                ))
                item["RecursiveItemCount"] = playable_episode_count
                item["MediaType"] = "Video"
                series.append(item)

        movies.sort(key=lambda item: item.get("Name", "").casefold())
        series.sort(key=lambda item: item.get("Name", "").casefold())
        _CATALOG_CACHE = (time.monotonic(), signature, movies, series)
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
    _, source_data = await _source_access()
    if not _source_item_allowed(doc, source_data):
        # Do not disclose whether a restricted title exists.
        raise HTTPException(status_code=404, detail="Media item not found")
    return doc


async def _resolve_item(item_id: str) -> tuple[dict, dict, Optional[dict], Optional[dict]]:
    """Return (payload, parent document, season, episode) for a stable Jellyfin item id."""
    global _CATALOG_CACHE
    try:
        payload = _decode_item_id(item_id)
    except HTTPException:
        # A process restart clears the in-memory UUID map. Loading the catalogue
        # deterministically rebuilds mappings for movies, shows, seasons and
        # playable episodes without persisting opaque source IDs to the client.
        _CATALOG_CACHE = None
        await _load_top_level_items()
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
        "RunTimeTicks": _runtime_ticks(episode.get("runtime") or doc.get("runtime")),
        "MediaType": "Video",
    })
    if episode.get("overview"):
        result["Overview"] = episode["overview"]
    if episode.get("released"):
        result["PremiereDate"] = str(episode["released"])
    return result


def _split_query_values(value: Any) -> list[str]:
    """Parse Jellyfin's comma- or pipe-delimited multi-value query fields."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_values = [str(item) for item in value if item is not None]
    else:
        raw_values = [str(value)]
    return [part.strip() for raw in raw_values for part in re.split(r"[,|]", raw) if part.strip()]


def _as_optional_bool(value: Optional[bool]) -> Optional[bool]:
    return value if isinstance(value, bool) else None


async def _items_for_parent(
    parent_id: Optional[str],
    include_types: Optional[str],
    search_term: Optional[str],
    start: int,
    limit: int,
    sort_by: Optional[str] = None,
    sort_order: str = "Ascending",
    filters: Optional[str] = None,
    genres: Optional[str] = None,
    years: Optional[str] = None,
    user_id: Optional[str] = None,
    media_types: Optional[str] = None,
    exclude_item_types: Optional[str] = None,
    is_favorite: Optional[bool] = None,
    is_played: Optional[bool] = None,
    person: Optional[str] = None,
    studios: Optional[str] = None,
    name_starts_with: Optional[str] = None,
    name_starts_with_or_greater: Optional[str] = None,
    name_less_than: Optional[str] = None,
    min_community_rating: Optional[float] = None,
) -> dict:
    movies, series = await _load_top_level_items()
    allowed = {part.casefold() for part in _split_query_values(include_types)}
    excluded = {part.casefold() for part in _split_query_values(exclude_item_types)}
    requested_media_types = {part.casefold() for part in _split_query_values(media_types)}
    results: list[dict] = []

    if not parent_id:
        if not allowed or "movie" in allowed:
            results.extend(movies)
        if not allowed or "series" in allowed:
            results.extend(series)
    elif parent_id == _ROOT_ID:
        results = [
            _view_item(_ROOT_MOVIES_ID, "Movies", "movies", len(movies)),
            _view_item(_ROOT_SERIES_ID, "TV Shows", "tvshows", len(series)),
        ]
    elif parent_id in (_ROOT_MOVIES_ID, _ROOT_SERIES_ID):
        results = list(movies if parent_id == _ROOT_MOVIES_ID else series)
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

    if requested_media_types and "video" not in requested_media_types:
        results = []
    if allowed:
        results = [item for item in results if str(item.get("Type", "")).casefold() in allowed]
    if excluded:
        results = [item for item in results if str(item.get("Type", "")).casefold() not in excluded]
    if search_term:
        needle = search_term.casefold()
        results = [item for item in results if needle in str(item.get("Name", "")).casefold()]
    if name_starts_with:
        needle = name_starts_with.casefold()
        results = [item for item in results if str(item.get("Name", "")).casefold().startswith(needle)]
    if name_starts_with_or_greater:
        needle = name_starts_with_or_greater.casefold()
        results = [item for item in results if str(item.get("SortName", item.get("Name", ""))).casefold() >= needle]
    if name_less_than:
        needle = name_less_than.casefold()
        results = [item for item in results if str(item.get("SortName", item.get("Name", ""))).casefold() < needle]

    # Filter by per-user Jellyfin state before applying user-state query filters.
    if user_id:
        results = await _apply_user_data(results, user_id)
    filter_set = {part.casefold() for part in _split_query_values(filters)}
    if "isresumable" in filter_set:
        results = [item for item in results if int((item.get("UserData") or {}).get("PlaybackPositionTicks") or 0) > 0 and not (item.get("UserData") or {}).get("Played")]
    if "isplayed" in filter_set:
        results = [item for item in results if bool((item.get("UserData") or {}).get("Played"))]
    if "isunplayed" in filter_set:
        results = [item for item in results if not bool((item.get("UserData") or {}).get("Played"))]
    if "isfavorite" in filter_set:
        results = [item for item in results if bool((item.get("UserData") or {}).get("IsFavorite"))]
    if "isfolder" in filter_set:
        results = [item for item in results if bool(item.get("IsFolder"))]
    if "isnotfolder" in filter_set:
        results = [item for item in results if not bool(item.get("IsFolder"))]
    favorite_filter = _as_optional_bool(is_favorite)
    if favorite_filter is not None:
        results = [item for item in results if bool((item.get("UserData") or {}).get("IsFavorite")) is favorite_filter]
    played_filter = _as_optional_bool(is_played)
    if played_filter is not None:
        results = [item for item in results if bool((item.get("UserData") or {}).get("Played")) is played_filter]

    selected_genres = {part.casefold() for part in _split_query_values(genres)}
    if selected_genres:
        results = [item for item in results if selected_genres.intersection({str(g).casefold() for g in item.get("Genres", [])})]
    selected_years = set(_split_query_values(years))
    if selected_years:
        results = [item for item in results if str(item.get("ProductionYear") or "") in selected_years]
    selected_studios = {part.casefold() for part in _split_query_values(studios)}
    if selected_studios:
        results = [item for item in results if selected_studios.intersection({str(studio).casefold() for studio in item.get("Studios", [])})]
    if person:
        person_needle = person.casefold()
        results = [item for item in results if any(
            person_needle in str(person_item.get("Name") or "").casefold()
            for person_item in (item.get("People") or []) if isinstance(person_item, dict)
        )]
    if min_community_rating is not None:
        results = [item for item in results if item.get("CommunityRating") is not None and float(item.get("CommunityRating") or 0) >= min_community_rating]

    # Sort the complete matching set before pagination. Sorting only the page
    # causes inconsistent results when users navigate beyond the first page.
    sort_keys = _split_query_values(sort_by)
    descending = str(sort_order).casefold() in {"descending", "desc"}
    key_map = {
        "SortName": "SortName", "Name": "Name", "DateCreated": "DateCreated",
        "ProductionYear": "ProductionYear", "CommunityRating": "CommunityRating",
        "IndexNumber": "IndexNumber", "ParentIndexNumber": "ParentIndexNumber",
    }
    for requested_key in reversed(sort_keys):
        field = key_map.get(requested_key)
        if not field:
            continue
        if field in {"ProductionYear", "CommunityRating", "IndexNumber", "ParentIndexNumber"}:
            results.sort(key=lambda item: (item.get(field) is None, item.get(field) or 0), reverse=descending)
        else:
            results.sort(key=lambda item: str(item.get(field) or "").casefold(), reverse=descending)

    total = len(results)
    start = max(0, start)
    limit = max(1, min(limit, 200))
    return {"Items": results[start:start + limit], "TotalRecordCount": total, "StartIndex": start}


async def _apply_user_data(items: list[dict], user_id: str) -> list[dict]:
    """Overlay per-user resume/watched/favorite state without changing catalogue records."""
    if not items:
        return items
    item_ids = [str(item.get("Id")) for item in items if item.get("Id")]
    if not item_ids:
        return items
    cursor = _tracking_collection("jellyfin_playback_state").find(
        {"user_id": user_id, "item_id": {"$in": item_ids}}
    )
    states = await cursor.to_list(length=len(item_ids))
    state_by_id = {str(state.get("item_id")): state for state in states}
    for item in items:
        state = state_by_id.get(str(item.get("Id")))
        if not state:
            continue
        item["UserData"] = {
            "PlaybackPositionTicks": int(state.get("position_ticks") or 0),
            "PlayCount": 1 if state.get("played") else 0,
            "IsFavorite": bool(state.get("is_favorite", False)),
            "Played": bool(state.get("played", False)),
            "LastPlayedDate": _date_iso(state.get("updated_at")) if state.get("played") else None,
        }
    return items


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


@router.get("/Jellyfin/Compat/Health")
async def jellyfin_compat_health():
    """Safe deployment diagnostic; validates dependencies without exposing secrets."""
    _require_enabled()
    web_root = Path(__file__).resolve().parents[1] / "jellyfin_web"
    web_ready = (
        (web_root / "index.html").is_file()
        and any(web_root.rglob("*.js"))
        and any(web_root.rglob("*.css"))
    )

    source_token_configured = False
    source_token_valid = False
    try:
        configured = str(getattr(Telegram, "JELLYFIN_SOURCE_TOKEN", "") or "").strip()
        if not configured:
            configured = str(getattr(SettingsManager.current(), "strm_default_token", "") or "").strip()
        source_token_configured = bool(configured)
        if configured:
            await asyncio.wait_for(_source_access(), timeout=6)
            source_token_valid = True
    except HTTPException:
        source_token_valid = False
    except Exception:
        source_token_valid = False

    tracking_db = getattr(db, "dbs", {}).get("tracking")
    tracking_ready = False
    if tracking_db is not None:
        try:
            await asyncio.wait_for(tracking_db.command("ping"), timeout=3)
            tracking_ready = True
        except AttributeError:
            # Lightweight fake databases used by the isolated contract suite.
            tracking_ready = True
        except Exception:
            tracking_ready = False

    storage_keys = _storage_keys()
    storage_reachable = bool(storage_keys)
    for storage_key in storage_keys:
        try:
            await asyncio.wait_for(db.dbs[storage_key].command("ping"), timeout=3)
        except AttributeError:
            # Lightweight fake databases used by the isolated contract suite.
            continue
        except Exception:
            storage_reachable = False
            break

    telegram_stream_client_ready = False
    try:
        from Backend.pyrofork.bot import StreamBot
        telegram_stream_client_ready = bool(getattr(StreamBot, "is_connected", False))
    except Exception:
        # The isolated contract test does not load a real Pyrogram client.
        telegram_stream_client_ready = False

    return {
        "Enabled": True,
        "ServerName": _server_name(),
        "ServerId": _server_id(),
        "ApiVersion": "10.10.7",
        "WebClientAssetsPresent": web_ready,
        "ContentSourceTokenConfigured": source_token_configured,
        "ContentSourceTokenValid": source_token_valid,
        "TrackingDatabaseReachable": tracking_ready,
        "MediaStorageDatabaseCount": len(storage_keys),
        "MediaStorageDatabasesReachable": storage_reachable,
        "TelegramStreamClientConnected": telegram_stream_client_ready,
        "ReadyForAndroidClient": bool(
            web_ready and source_token_valid and tracking_ready
            and storage_reachable and telegram_stream_client_ready
        ),
    }


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
async def jellyfin_system_info():
    # Jellyfin Android checks System/Info during the initial server handshake,
    # before the user has supplied credentials. This DTO contains only public
    # server identity/capability information, so it must not require a session.
    _require_enabled()
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


@router.websocket("/socket")
async def jellyfin_websocket(websocket: WebSocket):
    """Jellyfin-compatible keep-alive socket.

    Jellyfin Android can open its socket while it is still connecting to the
    server, before a user session exists. Allow that anonymous handshake, but
    validate any token that is explicitly supplied. Authenticated sockets
    refresh their session; anonymous sockets are kept alive only temporarily
    and carry no privileged events or data.
    """
    token = (
        websocket.query_params.get("api_key")
        or websocket.query_params.get("ApiKey")
        or websocket.headers.get("X-Emby-Token")
        or websocket.headers.get("X-MediaBrowser-Token")
    )
    if not token:
        authorization = websocket.headers.get("Authorization", "")
        match = re.search(r'(?:MediaBrowser|Emby)\s+.*?Token="?([^",\s]+)', authorization, re.IGNORECASE)
        token = match.group(1) if match else None
    session = None
    if token:
        try:
            session = await _session_from_token(token)
        except HTTPException:
            # Android can retry a persisted/stale socket token while the user
            # is reconnecting. An invalid token must not block initial server
            # bootstrap; an anonymous socket receives keep-alives only and can
            # never access or receive private media/session data.
            session = None

    await websocket.accept()
    try:
        await websocket.send_json({"MessageType": "KeepAlive"})
        last_refresh = time.monotonic()
        anonymous_started = last_refresh
        while True:
            if session is None and time.monotonic() - anonymous_started > 600:
                await websocket.close(code=1000)
                return
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=25)
                # Clients send KeepAlive messages periodically. Other message
                # types are ignored because server-side remote control is not
                # part of this media-provider integration.
                try:
                    message = json.loads(raw)
                except Exception:
                    message = {}
                if not isinstance(message, dict):
                    message = {}
                if session is not None and time.monotonic() - last_refresh >= 60:
                    await _tracking_collection("jellyfin_sessions").update_one(
                        {"_id": session["_id"]},
                        {"$set": {"last_seen_at": datetime.now(timezone.utc)}},
                    )
                    last_refresh = time.monotonic()
            except asyncio.TimeoutError:
                await websocket.send_json({"MessageType": "KeepAlive"})
    except WebSocketDisconnect:
        return
    except Exception as exc:
        LOGGER.debug("[JELLYFIN] WebSocket closed: %s", exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


@router.get("/Branding/Configuration")
async def jellyfin_branding_configuration():
    _require_enabled()
    return {"LoginDisclaimer": "", "CustomCss": "", "SplashscreenEnabled": False}


@router.get("/System/Endpoint")
async def jellyfin_system_endpoint():
    _require_enabled()
    return {"IsLocal": False, "IsInNetwork": False}


@router.post("/System/Ping")
async def jellyfin_system_ping_post():
    _require_enabled()
    return Response(status_code=200)


@router.get("/System/Configuration/Public")
async def jellyfin_public_configuration():
    _require_enabled()
    return {
        "EnableHttps": True,
        "PublicHttpsPort": 443,
        "PublicPort": 443,
        "HttpServerPortNumber": 443,
        "HttpsPortNumber": 443,
        "EnableRemoteAccess": True,
        "EnableUPnP": False,
        "BaseUrl": "",
        "IsBehindProxy": True,
    }


@router.get("/System/Configuration")
async def jellyfin_system_configuration(request: Request):
    await _session_for_request(request)
    return {
        "Enable case-sensitive item ids": False,
        "UICulture": "en-US",
        "MetadataCountryCode": "US",
        "PreferredMetadataLanguage": "en",
        "EnableFolderView": False,
        "EnableGroupingIntoCollections": False,
        "EnableExternalContentInSuggestions": False,
        "EnableRemoteContentInSuggestions": False,
    }


@router.get("/QuickConnect/Enabled")
async def jellyfin_quick_connect_enabled():
    _require_enabled()
    return False


@router.get("/QuickConnect/Status")
async def jellyfin_quick_connect_status():
    _require_enabled()
    # Quick Connect is intentionally disabled in this first account model.
    # Clients can still use the ordinary username/password token exchange.
    return {"Authenticated": False, "Code": None, "Secret": None}


@router.post("/QuickConnect/Initiate")
async def jellyfin_quick_connect_initiate():
    _require_enabled()
    raise HTTPException(status_code=401, detail="Quick Connect is disabled; use username and password")


@router.get("/QuickConnect/Connect")
async def jellyfin_quick_connect_connect():
    _require_enabled()
    raise HTTPException(status_code=401, detail="Quick Connect is disabled; use username and password")


@router.post("/Users/AuthenticateWithQuickConnect")
async def jellyfin_quick_connect_authenticate():
    _require_enabled()
    raise HTTPException(status_code=401, detail="Quick Connect is disabled; use username and password")


@router.get("/Auth/Providers")
async def jellyfin_auth_providers():
    _require_enabled()
    # No external authentication provider is configured; password auth is the
    # single supported provider for this one-user compatibility deployment.
    return []


@router.get("/Auth/PasswordResetProviders")
async def jellyfin_password_reset_providers():
    _require_enabled()
    # Password reset is managed through Heroku Config Vars by the owner.
    return []


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
    plain_password = body.get("Pw")
    if plain_password in (None, ""):
        plain_password = body.get("pw")
    if plain_password in (None, ""):
        plain_password = body.get("password")
    if plain_password not in (None, ""):
        supplied_password = str(plain_password)
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
    # Playback tickets are bound to the session record, so deleting the
    # session immediately invalidates every outstanding video/subtitle URL.
    await _tracking_collection("jellyfin_sessions").delete_one({"token_hash": _token_hash(session["access_token"])})
    return Response(status_code=204)


@router.get("/Sessions")
async def jellyfin_sessions(request: Request, controllable_by_user_id: Optional[str] = Query(None, alias="ControllableByUserId")):
    session = await _session_for_request(request)
    if controllable_by_user_id and not _is_current_user(controllable_by_user_id):
        return []
    return [{
        "Id": str(session.get("_id") or ""),
        "ServerId": _server_id(),
        "UserId": session.get("user_id", _user_id()),
        "UserName": session.get("username", _username()),
        "Client": session.get("client", "Unknown client"),
        "DeviceName": session.get("device", "Unknown device"),
        "DeviceId": session.get("device_id", "unknown-device"),
        "ApplicationVersion": session.get("client_version", "unknown"),
        "LastActivityDate": datetime.now(timezone.utc).isoformat(),
        "IsActive": True,
        "SupportsRemoteControl": False,
        "PlayState": {},
        "AdditionalUsers": [],
    }]


@router.post("/Sessions/Capabilities/Full")
@router.post("/Sessions/Capabilities")
async def jellyfin_session_capabilities(request: Request):
    """Accept the client capability handshake before or after login.

    The Android client sends this request as soon as the server URL is
    accepted, before the login screen. Requiring an access token here creates
    a connection/login deadlock. When a valid session token is present, save
    the capabilities against that session; otherwise safely accept and ignore
    the anonymous bootstrap payload.
    """
    _require_enabled()
    try:
        capabilities = await request.json()
    except Exception:
        capabilities = {}
    if not isinstance(capabilities, dict):
        capabilities = {}

    auth = _parse_authorization_header(request)
    token = auth.get("token")
    if token:
        try:
            session = await _session_from_token(token)
        except HTTPException:
            # A persisted token from a previous installation may be stale. The
            # capabilities payload is non-sensitive and the official client can
            # send it before it has re-authenticated, so do not block bootstrap.
            session = None
        if session is not None:
            await _tracking_collection("jellyfin_sessions").update_one(
                {"_id": session["_id"]},
                {"$set": {"capabilities": capabilities, "capabilities_updated_at": datetime.now(timezone.utc)}},
            )
    return Response(status_code=204)


@router.get("/DisplayPreferences/{display_preferences_id}")
async def jellyfin_get_display_preferences(
    display_preferences_id: str,
    request: Request,
    user_id: Optional[str] = Query(None, alias="userId"),
    client: str = Query("emby", alias="client"),
):
    session = await _session_for_request(request)
    if user_id and not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    collection = _tracking_collection("jellyfin_display_preferences")
    saved = await collection.find_one({
        "user_id": session["user_id"],
        "preference_id": display_preferences_id,
        "client": client,
    })
    if saved and isinstance(saved.get("value"), dict):
        return saved["value"]
    return {
        "Id": display_preferences_id,
        "ShowSidebar": True,
        "RememberIndexing": False,
        "PrimaryImageHeight": 250,
        "PrimaryImageWidth": 250,
        "CustomPrefs": {},
        "ScrollDirection": "Horizontal",
        "ViewType": "Poster",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
    }


@router.post("/DisplayPreferences/{display_preferences_id}")
async def jellyfin_set_display_preferences(
    display_preferences_id: str,
    request: Request,
    user_id: Optional[str] = Query(None, alias="userId"),
    client: str = Query("emby", alias="client"),
):
    session = await _session_for_request(request)
    if user_id and not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    try:
        value = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected display preferences JSON")
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Display preferences must be an object")
    value["Id"] = display_preferences_id
    await _tracking_collection("jellyfin_display_preferences").update_one(
        {"user_id": session["user_id"], "preference_id": display_preferences_id, "client": client},
        {"$set": {
            "user_id": session["user_id"],
            "preference_id": display_preferences_id,
            "client": client,
            "value": value,
            "updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return Response(status_code=204)


@router.get("/Items/Root")
async def jellyfin_root_item(request: Request):
    """Return the stable virtual root folder used by Jellyfin item navigation."""
    await _session_for_request(request)
    return {
        "Name": "Media",
        "ServerId": _server_id(),
        "Id": _ROOT_ID,
        "Type": "Folder",
        "IsFolder": True,
        "CanDelete": False,
        "CanDownload": False,
        "ChildCount": 2,
        "SortName": "Media",
        "PresentationUniqueKey": _ROOT_ID,
    }


@router.get("/UserViews/GroupingOptions")
async def jellyfin_global_grouping_options(request: Request):
    await _session_for_request(request)
    return []


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


async def _items_response(
    request: Request,
    parent_id: Optional[str],
    include_item_types: Optional[str],
    search_term: Optional[str],
    start_index: int,
    limit: int,
    sort_by: Optional[str] = None,
    sort_order: str = "Ascending",
    filters: Optional[str] = None,
    genres: Optional[str] = None,
    years: Optional[str] = None,
    media_types: Optional[str] = None,
    exclude_item_types: Optional[str] = None,
    is_favorite: Optional[bool] = None,
    is_played: Optional[bool] = None,
    person: Optional[str] = None,
    studios: Optional[str] = None,
    name_starts_with: Optional[str] = None,
    name_starts_with_or_greater: Optional[str] = None,
    name_less_than: Optional[str] = None,
    min_community_rating: Optional[float] = None,
):
    session = await _session_for_request(request)
    return await _items_for_parent(
        parent_id=parent_id,
        include_types=include_item_types,
        search_term=search_term,
        start=start_index,
        limit=limit,
        sort_by=sort_by,
        sort_order=sort_order,
        filters=filters,
        genres=genres,
        years=years,
        user_id=session["user_id"],
        media_types=media_types,
        exclude_item_types=exclude_item_types,
        is_favorite=is_favorite,
        is_played=is_played,
        person=person,
        studios=studios,
        name_starts_with=name_starts_with,
        name_starts_with_or_greater=name_starts_with_or_greater,
        name_less_than=name_less_than,
        min_community_rating=min_community_rating,
    )


def _items_query_parameters(
    parent_id: Optional[str],
    include_item_types: Optional[str],
    search_term: Optional[str],
    start_index: int,
    limit: int,
    sort_by: Optional[str],
    sort_order: str,
    filters: Optional[str],
    genres: Optional[str],
    years: Optional[str],
    media_types: Optional[str],
    exclude_item_types: Optional[str],
    is_favorite: Optional[bool],
    is_played: Optional[bool],
    person: Optional[str],
    studios: Optional[str],
    name_starts_with: Optional[str],
    name_starts_with_or_greater: Optional[str],
    name_less_than: Optional[str],
    min_community_rating: Optional[float],
) -> tuple:
    """Keep the two Jellyfin GetItems routes on the same query contract."""
    return (
        parent_id, include_item_types, search_term, start_index, limit,
        sort_by, sort_order, filters, genres, years, media_types,
        exclude_item_types, is_favorite, is_played, person, studios,
        name_starts_with, name_starts_with_or_greater, name_less_than,
        min_community_rating,
    )


@router.get("/Items")
async def jellyfin_items(
    request: Request,
    parent_id: Optional[str] = Query(None, alias="ParentId"),
    include_item_types: Optional[str] = Query(None, alias="IncludeItemTypes"),
    search_term: Optional[str] = Query(None, alias="SearchTerm"),
    start_index: int = Query(0, alias="StartIndex", ge=0),
    limit: int = Query(100, alias="Limit", ge=1, le=200),
    recursive: bool = Query(True, alias="Recursive"),
    sort_by: Optional[str] = Query(None, alias="SortBy"),
    sort_order: str = Query("Ascending", alias="SortOrder"),
    filters: Optional[str] = Query(None, alias="Filters"),
    genres: Optional[str] = Query(None, alias="Genres"),
    years: Optional[str] = Query(None, alias="Years"),
    media_types: Optional[str] = Query(None, alias="MediaTypes"),
    exclude_item_types: Optional[str] = Query(None, alias="ExcludeItemTypes"),
    is_favorite: Optional[bool] = Query(None, alias="IsFavorite"),
    is_played: Optional[bool] = Query(None, alias="IsPlayed"),
    person: Optional[str] = Query(None, alias="Person"),
    studios: Optional[str] = Query(None, alias="Studios"),
    name_starts_with: Optional[str] = Query(None, alias="NameStartsWith"),
    name_starts_with_or_greater: Optional[str] = Query(None, alias="NameStartsWithOrGreater"),
    name_less_than: Optional[str] = Query(None, alias="NameLessThan"),
    min_community_rating: Optional[float] = Query(None, alias="MinCommunityRating", ge=0, le=10),
):
    query_values = _items_query_parameters(
        parent_id, include_item_types, search_term, start_index, limit, sort_by, sort_order,
        filters, genres, years, media_types, exclude_item_types, is_favorite, is_played,
        person, studios, name_starts_with, name_starts_with_or_greater, name_less_than,
        min_community_rating,
    )
    return await _items_response(request, *query_values)


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
    sort_by: Optional[str] = Query(None, alias="SortBy"),
    sort_order: str = Query("Ascending", alias="SortOrder"),
    filters: Optional[str] = Query(None, alias="Filters"),
    genres: Optional[str] = Query(None, alias="Genres"),
    years: Optional[str] = Query(None, alias="Years"),
    media_types: Optional[str] = Query(None, alias="MediaTypes"),
    exclude_item_types: Optional[str] = Query(None, alias="ExcludeItemTypes"),
    is_favorite: Optional[bool] = Query(None, alias="IsFavorite"),
    is_played: Optional[bool] = Query(None, alias="IsPlayed"),
    person: Optional[str] = Query(None, alias="Person"),
    studios: Optional[str] = Query(None, alias="Studios"),
    name_starts_with: Optional[str] = Query(None, alias="NameStartsWith"),
    name_starts_with_or_greater: Optional[str] = Query(None, alias="NameStartsWithOrGreater"),
    name_less_than: Optional[str] = Query(None, alias="NameLessThan"),
    min_community_rating: Optional[float] = Query(None, alias="MinCommunityRating", ge=0, le=10),
):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    query_values = _items_query_parameters(
        parent_id, include_item_types, search_term, start_index, limit, sort_by, sort_order,
        filters, genres, years, media_types, exclude_item_types, is_favorite, is_played,
        person, studios, name_starts_with, name_starts_with_or_greater, name_less_than,
        min_community_rating,
    )
    return await _items_response(request, *query_values)


@router.get("/Users/{user_id}/Items/Latest")
async def jellyfin_latest_items(
    user_id: str, request: Request,
    limit: int = Query(20, alias="Limit", ge=1, le=100),
    parent_id: Optional[str] = Query(None, alias="ParentId"),
):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    await _session_for_request(request)
    movies, series = await _load_top_level_items()
    if parent_id == _ROOT_MOVIES_ID:
        items = list(movies)
    elif parent_id == _ROOT_SERIES_ID:
        items = list(series)
    elif parent_id:
        payload, doc, season_obj, _ = await _resolve_item(parent_id)
        if payload.get("collection") == "tv" and payload.get("episode") is None:
            selected = [season_obj] if season_obj else (doc.get("seasons") or [])
            items = []
            for season_doc in selected:
                for episode_doc in season_doc.get("episodes") or []:
                    if _best_quality(episode_doc.get("telegram")):
                        items.append(_episode_item(payload["db"], doc, season_doc, episode_doc))
        else:
            items = []
    else:
        items = list(movies + series)
    items = sorted(items, key=lambda item: item.get("DateCreated") or "", reverse=True)
    return await _apply_user_data(items[:limit], _user_id())


@router.get("/Users/{user_id}/Items/Resume")
async def jellyfin_resume_items(
    user_id: str, request: Request,
    limit: int = Query(20, alias="Limit", ge=1, le=100),
    media_types: Optional[str] = Query(None, alias="MediaTypes"),
):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    session = await _session_for_request(request)
    if media_types and "video" not in {part.strip().casefold() for part in media_types.split(",") if part.strip()}:
        return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
    collection = _tracking_collection("jellyfin_playback_state")
    cursor = collection.find({"user_id": session["user_id"], "played": False}).sort("updated_at", -1)
    raw_states = await cursor.to_list(length=5000)
    states = [state for state in raw_states if int(state.get("position_ticks") or 0) > 0][:limit]
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


@router.get("/Users/{user_id}/Items/Played")
async def jellyfin_played_items(user_id: str, request: Request, limit: int = Query(100, alias="Limit", ge=1, le=200)):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    session = await _session_for_request(request)
    cursor = _tracking_collection("jellyfin_playback_state").find(
        {"user_id": session["user_id"], "played": True}
    ).sort("updated_at", -1).limit(limit)
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
                "PlaybackPositionTicks": 0,
                "Played": True,
                "PlayCount": 1,
                "IsFavorite": bool(state.get("is_favorite", False)),
                "LastPlayedDate": _date_iso(state.get("updated_at")),
            }
            results.append(item)
        except HTTPException:
            continue
    return {"Items": results, "TotalRecordCount": len(results), "StartIndex": 0}


@router.get("/UserItems/Resume")
async def jellyfin_resume_items_global(
    request: Request,
    limit: int = Query(12, alias="Limit", ge=1, le=100),
    media_types: Optional[str] = Query(None, alias="MediaTypes"),
):
    return await jellyfin_resume_items(_user_id(), request, limit, media_types)


@router.get("/UserItems/Latest")
async def jellyfin_latest_items_global(
    request: Request,
    limit: int = Query(16, alias="Limit", ge=1, le=100),
    parent_id: Optional[str] = Query(None, alias="ParentId"),
):
    return await jellyfin_latest_items(_user_id(), request, limit, parent_id)


@router.get("/UserItems/Played")
async def jellyfin_played_items_global(
    request: Request, limit: int = Query(100, alias="Limit", ge=1, le=200)
):
    return await jellyfin_played_items(_user_id(), request, limit)


@router.get("/Users/{user_id}/Configuration")
async def jellyfin_user_configuration(user_id: str, request: Request):
    await _session_for_request(request)
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "AudioLanguagePreference": "", "PlayDefaultAudioTrack": True,
        "SubtitleLanguagePreference": "", "DisplayMissingEpisodes": False,
        "GroupedFolders": [], "SubtitleMode": "Default", "DisplayCollectionsView": False,
        "EnableLocalPassword": False, "OrderedViews": [], "LatestItemsExcludes": [],
    }


async def _set_item_user_flag(item_id: str, request: Request, *, played: Optional[bool] = None, favorite: Optional[bool] = None):
    session = await _session_for_request(request)
    # Resolve and permission-check the title before creating user state.
    payload, doc, season, episode = await _resolve_item(item_id)
    if payload.get("collection") == "movie":
        pass
    elif episode is None:
        raise HTTPException(status_code=400, detail="Only movies and episodes support playback user data")
    update = {
        "user_id": session["user_id"],
        "item_id": item_id,
        "updated_at": datetime.now(timezone.utc),
    }
    if played is not None:
        update["played"] = played
        if played:
            update["position_ticks"] = 0
    if favorite is not None:
        update["is_favorite"] = favorite
    await _tracking_collection("jellyfin_playback_state").update_one(
        {"user_id": session["user_id"], "item_id": item_id},
        {"$set": update},
        upsert=True,
    )


@router.get("/Users/{user_id}/Items/{item_id}/UserData")
@router.get("/UserItems/{item_id}/UserData")
async def jellyfin_get_item_user_data(item_id: str, request: Request, user_id: Optional[str] = None):
    session = await _session_for_request(request)
    if user_id and not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    await _resolve_item(item_id)
    state = await _tracking_collection("jellyfin_playback_state").find_one({"user_id": session["user_id"], "item_id": item_id}) or {}
    played = bool(state.get("played", False))
    return {
        "PlaybackPositionTicks": 0 if played else int(state.get("position_ticks") or 0),
        "PlayCount": 1 if played else 0,
        "IsFavorite": bool(state.get("is_favorite", False)),
        "Played": played,
        "LastPlayedDate": _date_iso(state.get("updated_at")) if played else None,
    }


@router.post("/Users/{user_id}/PlayedItems/{item_id}")
@router.post("/PlayedItems/{item_id}")
async def jellyfin_mark_played(item_id: str, request: Request, user_id: Optional[str] = None):
    if user_id and not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    await _set_item_user_flag(item_id, request, played=True)
    return Response(status_code=200)


@router.delete("/Users/{user_id}/PlayedItems/{item_id}")
@router.delete("/PlayedItems/{item_id}")
async def jellyfin_mark_unplayed(item_id: str, request: Request, user_id: Optional[str] = None):
    if user_id and not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    await _set_item_user_flag(item_id, request, played=False)
    return Response(status_code=200)


@router.post("/UserFavoriteItems/{item_id}")
@router.delete("/UserFavoriteItems/{item_id}")
async def jellyfin_favorite_item(item_id: str, request: Request):
    await _set_item_user_flag(item_id, request, favorite=request.method == "POST")
    return Response(status_code=200)


@router.post("/Users/{user_id}/FavoriteItems/{item_id}")
@router.delete("/Users/{user_id}/FavoriteItems/{item_id}")
async def jellyfin_user_favorite_item(user_id: str, item_id: str, request: Request):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    await _set_item_user_flag(item_id, request, favorite=request.method == "POST")
    return Response(status_code=200)


@router.get("/Users/{user_id}/FavoriteItems")
async def jellyfin_favorite_items(user_id: str, request: Request, limit: int = Query(100, alias="Limit", ge=1, le=200)):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    session = await _session_for_request(request)
    cursor = _tracking_collection("jellyfin_playback_state").find({"user_id": session["user_id"], "is_favorite": True}).limit(limit)
    states = await cursor.to_list(length=limit)
    items = []
    for state in states:
        try:
            payload, doc, season_obj, episode_obj = await _resolve_item(str(state.get("item_id") or ""))
            if payload.get("collection") == "movie":
                items.append(_item_common(str(state["item_id"]), _display_name(doc), "Movie", doc))
            elif episode_obj:
                items.append(_episode_item(payload["db"], doc, season_obj, episode_obj))
        except HTTPException:
            continue
    return {"Items": await _apply_user_data(items, session["user_id"]), "TotalRecordCount": len(items), "StartIndex": 0}


@router.get("/Users/{user_id}/Items/{item_id}")
async def jellyfin_user_item(user_id: str, item_id: str, request: Request):
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return await jellyfin_item(item_id, request)


@router.get("/Items/Latest")
async def jellyfin_latest_items_api(
    request: Request,
    limit: int = Query(20, alias="Limit", ge=1, le=100),
    parent_id: Optional[str] = Query(None, alias="ParentId"),
):
    """Jellyfin's global latest-items endpoint returns a bare item array."""
    return await jellyfin_latest_items(_user_id(), request, limit, parent_id)


@router.get("/Items/Counts")
async def jellyfin_item_counts(request: Request):
    await _session_for_request(request)
    movies, series = await _load_top_level_items()
    return {
        "MovieCount": len(movies),
        "SeriesCount": len(series),
        "EpisodeCount": sum(int(item.get("RecursiveItemCount") or 0) for item in series),
        "ArtistCount": 0, "ProgramCount": 0, "TrailerCount": 0,
    }


@router.get("/Items/{item_id}")
async def jellyfin_item(item_id: str, request: Request):
    # Keep these static API paths working even though FastAPI's dynamic route
    # is registered earlier in this router (Jellyfin itself uses static
    # endpoints such as /Items/Filters).
    if item_id.casefold() in {"filters", "filters2"}:
        return await jellyfin_item_filters(request, request.query_params.get("ParentId"))
    if item_id.casefold() == "counts":
        return await jellyfin_item_counts(request)
    await _session_for_request(request)
    payload, doc, season, episode = await _resolve_item(item_id)
    if payload.get("collection") == "movie":
        result = _item_common(item_id, _display_name(doc), "Movie", doc)
        result["RunTimeTicks"] = _runtime_ticks(doc.get("runtime"))
    elif payload.get("episode") is not None and season and episode:
        result = _episode_item(payload["db"], doc, season, episode)
    elif payload.get("season") is not None and season:
        result = _season_item(payload["db"], doc, season)
    else:
        movies, series = await _load_top_level_items()
        result = next((item for item in series if item["Id"] == item_id), _item_common(item_id, _display_name(doc), "Series", doc))
    result_list = await _apply_user_data([result], _user_id())
    return result_list[0]




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


@router.get("/Items/Filters")
@router.get("/Items/Filters2")
async def jellyfin_item_filters(request: Request, parent_id: Optional[str] = Query(None, alias="ParentId")):
    await _session_for_request(request)
    movies, series = await _load_top_level_items()
    records = list(movies + series)
    if parent_id == _ROOT_MOVIES_ID:
        records = movies
    elif parent_id == _ROOT_SERIES_ID:
        records = series
    genres = sorted({str(genre) for item in records for genre in (item.get("Genres") or []) if genre}, key=str.casefold)
    years = sorted({int(item["ProductionYear"]) for item in records if item.get("ProductionYear")}, reverse=True)
    studios = sorted({str(studio) for item in records for studio in (item.get("Studios") or []) if studio}, key=str.casefold)
    people = sorted({
        str(person.get("Name"))
        for item in records
        for person in (item.get("People") or [])
        if isinstance(person, dict) and person.get("Name")
    }, key=str.casefold)
    return {
        "Genres": genres,
        "Tags": [],
        "OfficialRatings": [],
        "Years": years,
        "Studios": studios,
        "Person": people,
    }


@router.get("/Genres")
async def jellyfin_genres(request: Request, parent_id: Optional[str] = Query(None, alias="ParentId")):
    filters = await jellyfin_item_filters(request, parent_id)
    return {
        "Items": [{"Name": name, "Id": quote(name, safe="")} for name in filters["Genres"]],
        "TotalRecordCount": len(filters["Genres"]),
        "StartIndex": 0,
    }


@router.get("/Years")
async def jellyfin_years(request: Request, parent_id: Optional[str] = Query(None, alias="ParentId")):
    await _session_for_request(request)
    movies, series = await _load_top_level_items()
    items = list(movies + series)
    if parent_id == _ROOT_MOVIES_ID:
        items = movies
    elif parent_id == _ROOT_SERIES_ID:
        items = series
    years = sorted({int(item["ProductionYear"]) for item in items if item.get("ProductionYear")}, reverse=True)
    return {"Items": [{"Name": str(year), "Id": str(year)} for year in years], "TotalRecordCount": len(years), "StartIndex": 0}


@router.get("/Studios")
async def jellyfin_studios(request: Request, parent_id: Optional[str] = Query(None, alias="ParentId")):
    await _session_for_request(request)
    movies, series = await _load_top_level_items()
    names = sorted({str(studio) for item in movies + series for studio in (item.get("Studios") or []) if studio}, key=str.casefold)
    return {"Items": [{"Name": name, "Id": quote(name, safe="")} for name in names], "TotalRecordCount": len(names), "StartIndex": 0}


@router.get("/Persons")
async def jellyfin_persons(request: Request, search_term: Optional[str] = Query(None, alias="SearchTerm")):
    await _session_for_request(request)
    movies, series = await _load_top_level_items()
    _, source_data = await _source_access()
    people = {}
    # Re-read cast data only for matches when searching. Cast is metadata, not a
    # separate library type in this backend.
    for db_key in _storage_keys():
        for collection_name in ("movie", "tv"):
            async for doc in db.dbs[db_key][collection_name].find({"$and": [_source_visibility_query(source_data), _source_exclusive_query()]}, {"cast": 1}):
                if not _source_item_allowed(doc, source_data):
                    continue
                for person in doc.get("cast") or []:
                    if isinstance(person, str):
                        name, role = person, ""
                    elif isinstance(person, dict):
                        name = str(person.get("name") or person.get("Name") or "").strip()
                        role = str(person.get("character") or person.get("role") or "")
                    else:
                        continue
                    if name and (not search_term or search_term.casefold() in name.casefold()):
                        people[name.casefold()] = {"Name": name, "Id": quote(name, safe=""), "Type": "Actor", "Role": role}
    results = sorted(people.values(), key=lambda person: person["Name"].casefold())[:100]
    return {"Items": results, "TotalRecordCount": len(results), "StartIndex": 0}


@router.get("/LiveTv/Info")
async def jellyfin_livetv_info(request: Request):
    await _session_for_request(request)
    # This compatibility service is a movie/TV catalogue, not a tuner/DVR.
    # Jellyfin Web uses this capability DTO to decide whether to show Live TV
    # navigation. Explicitly report it as unavailable rather than returning a
    # 404 or accidentally exposing controls that cannot work.
    return {"IsEnabled": False, "Services": [], "EnabledUsers": []}


@router.get("/LiveTv/Programs/Recommended")
async def jellyfin_livetv_recommended(request: Request):
    await _session_for_request(request)
    # This provider has no Live TV tuner or programme guide. Return a valid
    # empty Jellyfin result rather than a 404 on the home dashboard.
    return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}


@router.get("/Shows/NextUp")
async def jellyfin_next_up(
    request: Request,
    limit: int = Query(24, alias="Limit", ge=1, le=100),
    user_id: Optional[str] = Query(None, alias="UserId"),
    start_index: int = Query(0, alias="StartIndex", ge=0),
    series_id: Optional[str] = Query(None, alias="SeriesId"),
    parent_id: Optional[str] = Query(None, alias="ParentId"),
    enable_resumable: bool = Query(True, alias="EnableResumable"),
    enable_rewatching: bool = Query(False, alias="EnableRewatching"),
    disable_first_episode: bool = Query(False, alias="DisableFirstEpisode"),
    next_up_date_cutoff: Optional[str] = Query(None, alias="NextUpDateCutoff"),
    enable_total_record_count: bool = Query(True, alias="EnableTotalRecordCount"),
):
    session = await _session_for_request(request)
    if user_id and not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")

    state_cursor = _tracking_collection("jellyfin_playback_state").find(
        {"user_id": session["user_id"]}
    )
    user_states = await state_cursor.to_list(length=20000)
    watched_ids = {
        str(state.get("item_id")) for state in user_states if state.get("played")
    }
    resumable_positions = {
        str(state.get("item_id")): int(state.get("position_ticks") or 0)
        for state in user_states
        if not state.get("played") and int(state.get("position_ticks") or 0) > 0
    }

    cutoff = None
    if next_up_date_cutoff:
        try:
            cutoff = datetime.fromisoformat(next_up_date_cutoff.replace("Z", "+00:00"))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid NextUpDateCutoff")

    _, series = await _load_top_level_items()
    if series_id:
        series = [item for item in series if item.get("Id") == series_id]
    if parent_id and parent_id not in (_ROOT_SERIES_ID, _ROOT_ID, ""):
        series = [item for item in series if item.get("Id") == parent_id]

    next_items = []
    for series_item in series:
        try:
            payload, doc, _, _ = await _resolve_item(series_item["Id"])
        except HTTPException:
            continue

        episodes: list[tuple[dict, dict]] = []
        for season_doc in sorted(doc.get("seasons") or [], key=lambda x: int(x.get("season_number") or 0)):
            for episode_doc in sorted(season_doc.get("episodes") or [], key=lambda x: int(x.get("episode_number") or 0)):
                if not _best_quality(episode_doc.get("telegram")):
                    continue
                released = episode_doc.get("released") or episode_doc.get("release_date") or episode_doc.get("air_date")
                if cutoff and released:
                    try:
                        released_dt = datetime.fromisoformat(str(released).replace("Z", "+00:00"))
                        if released_dt.tzinfo is None:
                            released_dt = released_dt.replace(tzinfo=timezone.utc)
                        if released_dt < cutoff:
                            continue
                    except ValueError:
                        # Unknown/non-ISO dates are retained rather than silently
                        # hiding catalogue episodes.
                        pass
                episodes.append((season_doc, episode_doc))

        if not episodes:
            continue

        def _candidate(pair: tuple[dict, dict]) -> Optional[dict]:
            season_doc, episode_doc = pair
            season_number = int(season_doc.get("season_number") or 0)
            episode_number = int(episode_doc.get("episode_number") or 0)
            if disable_first_episode and season_number == 1 and episode_number == 1:
                return None
            return _episode_item(payload["db"], doc, season_doc, episode_doc)

        found = None
        # Prefer an unfinished episode when Jellyfin asks for resumable items.
        if enable_resumable:
            for pair in episodes:
                candidate = _candidate(pair)
                if candidate and resumable_positions.get(candidate["Id"], 0) > 0:
                    found = candidate
                    break

        # Otherwise choose the first episode not marked as fully watched.
        if found is None:
            for pair in episodes:
                candidate = _candidate(pair)
                if not candidate:
                    continue
                # When resumable episodes are disabled, do not surface an
                # unfinished episode in the ordinary next-unwatched position.
                if candidate["Id"] in resumable_positions and not enable_resumable:
                    continue
                if candidate["Id"] not in watched_ids:
                    found = candidate
                    break

        # Rewatching is only a fallback when no unfinished/unwatched episode
        # exists for the series.
        if found is None and enable_rewatching:
            for pair in episodes:
                candidate = _candidate(pair)
                if candidate and (enable_resumable or candidate["Id"] not in resumable_positions):
                    found = candidate
                    break

        if found:
            next_items.append(found)
        if len(next_items) >= limit:
            break

    total = len(next_items)
    next_items = next_items[start_index:start_index + limit]
    next_items = await _apply_user_data(next_items, session["user_id"])
    response = {"Items": next_items, "StartIndex": start_index}
    response["TotalRecordCount"] = total if enable_total_record_count else 0
    return response

@router.get("/Shows/Upcoming")
async def jellyfin_upcoming_shows(request: Request, limit: int = Query(24, alias="Limit", ge=1, le=100)):
    await _session_for_request(request)
    # The catalogue stores released episodes, not a scheduled airing calendar.
    # Return a valid empty result instead of inventing future air dates.
    return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}


@router.get("/Playback/BitrateTest")
async def jellyfin_bitrate_test(
    request: Request,
    size: int = Query(500_000, alias="Size", ge=1, le=5_000_000),
):
    await _session_for_request(request)
    # Jellyfin Web measures connectivity by downloading a small payload.
    # Use a bounded body to keep this compatibility endpoint predictable.
    return Response(content=b"\0" * size, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})


async def _subtitle_streams_for_item(
    *, item_id: str, media_source_id: str, request: Request, session: dict,
    payload: dict, doc: dict, season: Optional[dict], episode: Optional[dict],
) -> list[dict]:
    """Expose existing Telegram-Stremio sidecar subtitles as Jellyfin tracks."""
    imdb_id = str(doc.get("imdb_id") or "").strip()
    if not imdb_id:
        return []
    media_type = "tv" if payload.get("collection") == "tv" else "movie"
    season_number = payload.get("season") if media_type == "tv" else None
    episode_number = payload.get("episode") if media_type == "tv" else None
    if media_type == "tv" and (season_number is None or episode_number is None):
        return []

    try:
        subtitles = await get_subtitles_for(imdb_id, media_type, season_number, episode_number)
    except Exception as exc:
        # Subtitle indexing must never make the video itself unavailable.
        LOGGER.warning("[JELLYFIN] Subtitle lookup failed for item %s: %s", item_id, exc)
        return []

    subtitles = sorted(
        (sub for sub in subtitles if sub.get("encoded") and sub.get("name")),
        key=lambda sub: (
            str(sub.get("lang_label") or "Unknown").casefold(),
            str(sub.get("name") or "").casefold(),
            int(sub.get("msg_id") or 0),
        ),
    )
    base_url = str(request.base_url).rstrip("/")
    streams: list[dict] = []
    for index, sub in enumerate(subtitles):
        ext = subtitle_ext(str(sub.get("name") or "subtitle.srt"))
        language = str(sub.get("lang_code") or "und").lower()
        language_label = str(sub.get("lang_label") or "Unknown")
        title = str(sub.get("name") or f"{language_label}{ext}")
        forced = bool(re.search(r"(?:^|[ ._-])forced(?:$|[ ._-])", title, re.IGNORECASE))
        playback_ticket = await _create_playback_ticket(
            session=session, item_id=item_id, purpose="subtitle", track_index=index,
        )
        delivery_url = (
            f"{base_url}/Videos/{quote(item_id, safe='')}/{quote(media_source_id, safe='')}"
            f"/Subtitles/{index}/Stream{ext}?api_key={quote(playback_ticket, safe='')}"
        )
        streams.append({
            "Codec": ext.lstrip("."),
            "Language": language,
            "DisplayTitle": language_label + (" (Forced)" if forced else ""),
            "Title": title,
            "Type": "Subtitle",
            "Index": index,
            "IsExternal": True,
            "IsExternalUrl": False,
            "IsTextSubtitleStream": True,
            "IsDefault": False,
            "IsForced": forced,
            "SupportsExternalStream": True,
            "DeliveryMethod": "External",
            "DeliveryUrl": delivery_url,
        })
    return streams


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
    media_source_id = hashlib.sha256(f"{item_id}:{quality.get('id')}".encode()).hexdigest()[:32]
    subtitle_streams = await _subtitle_streams_for_item(
        item_id=item_id,
        media_source_id=media_source_id,
        request=request,
        session=session,
        payload=payload,
        doc=doc,
        season=season,
        episode=episode,
    )
    # Native players often open this URL outside the API Authorization context.
    # Use a short-lived, item-scoped ticket instead of leaking the long-lived
    # Jellyfin session token in the URL/query string.
    playback_ticket = await _create_playback_ticket(
        session=session, item_id=item_id, purpose="video",
    )
    stream_url = (
        str(request.base_url).rstrip("/")
        + f"/Videos/{quote(item_id, safe='')}/stream?api_key={quote(playback_ticket, safe='')}"
    )
    return {
        "PlaySessionId": secrets.token_hex(16),
        "ItemId": item_id,
        "MediaSources": [{
            "Protocol": "Http",
            "Id": media_source_id,
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
            "MediaStreams": subtitle_streams,
            "Bitrate": None,
            "Size": None,
        }],
        "ErrorCode": None,
        "SessionId": str(session.get("_id") or ""),
    }


@router.get("/Search/Hints")
async def jellyfin_search_hints(
    request: Request,
    search_term: str = Query("", alias="searchTerm", min_length=1, max_length=200),
    limit: int = Query(20, alias="Limit", ge=1, le=100),
):
    session = await _session_for_request(request)
    movies, series = await _load_top_level_items()
    needle = search_term.casefold()
    found = [item for item in movies + series if needle in str(item.get("Name", "")).casefold()]
    found = await _apply_user_data(found[:limit], session["user_id"])
    return {"SearchHints": found, "TotalRecordCount": len(found)}


@router.get("/Users/{user_id}/GroupingOptions")
async def jellyfin_grouping_options(user_id: str, request: Request):
    await _session_for_request(request)
    if not _is_current_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return []


@router.get("/Items/{item_id}/Similar")
async def jellyfin_similar_items(
    item_id: str, request: Request, limit: int = Query(12, alias="Limit", ge=1, le=50)
):
    session = await _session_for_request(request)
    payload, doc, _, _ = await _resolve_item(item_id)
    movies, series = await _load_top_level_items()
    pool = movies if payload.get("collection") == "movie" else series
    current_genres = {str(g).casefold() for g in (doc.get("genres") or [])}
    candidates = [item for item in pool if item.get("Id") != item_id]
    if current_genres:
        candidates.sort(
            key=lambda item: len(current_genres.intersection({str(g).casefold() for g in item.get("Genres", [])})),
            reverse=True,
        )
    candidates = await _apply_user_data(candidates[:limit], session["user_id"])
    return {"Items": candidates, "TotalRecordCount": len(candidates), "StartIndex": 0}


async def _dispatch_jellyfin_stream(
    item_id: str, request: Request, media_source_id: Optional[str] = None,
):
    session = await _session_for_media_request(request, item_id=item_id, purpose="video")
    payload, doc, season, episode = await _resolve_item(item_id)
    if payload.get("collection") == "movie":
        quality = _best_quality(doc.get("telegram"))
    elif episode:
        quality = _best_quality(episode.get("telegram"))
    else:
        raise HTTPException(status_code=400, detail="Only movies and episodes can be streamed")
    if not quality or not quality.get("id"):
        raise HTTPException(status_code=404, detail="No playable Telegram file is available")
    expected_source_id = hashlib.sha256(f"{item_id}:{quality.get('id')}".encode()).hexdigest()[:32]
    if media_source_id and not hmac.compare_digest(str(media_source_id), expected_source_id):
        raise HTTPException(status_code=404, detail="Media source not found")

    source_token, source_token_data = await _source_access()

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


@router.get("/Videos/{item_id}/{media_source_id}/Subtitles/{index}/Stream.{subtitle_format}")
async def jellyfin_subtitle_stream(
    item_id: str, media_source_id: str, index: int, subtitle_format: str, request: Request,
):
    """Stream an indexed external subtitle using a scoped playback ticket."""
    await _session_for_media_request(
        request, item_id=item_id, purpose="subtitle", track_index=index,
    )
    payload, doc, season, episode = await _resolve_item(item_id)
    if payload.get("collection") == "movie":
        quality = _best_quality(doc.get("telegram"))
    elif episode:
        quality = _best_quality(episode.get("telegram"))
    else:
        raise HTTPException(status_code=400, detail="Subtitles are available only for playable movies and episodes")
    if not quality or not quality.get("id"):
        raise HTTPException(status_code=404, detail="Media source no longer exists")
    expected_source_id = hashlib.sha256(f"{item_id}:{quality.get('id')}".encode()).hexdigest()[:32]
    if not hmac.compare_digest(str(media_source_id), expected_source_id):
        raise HTTPException(status_code=404, detail="Media source not found")

    imdb_id = str(doc.get("imdb_id") or "").strip()
    if not imdb_id:
        raise HTTPException(status_code=404, detail="No subtitles are indexed for this item")
    media_type = "tv" if payload.get("collection") == "tv" else "movie"
    season_number = payload.get("season") if media_type == "tv" else None
    episode_number = payload.get("episode") if media_type == "tv" else None
    if media_type == "tv" and (season_number is None or episode_number is None):
        raise HTTPException(status_code=404, detail="Episode subtitle not found")

    try:
        subtitles = await get_subtitles_for(imdb_id, media_type, season_number, episode_number)
    except Exception as exc:
        LOGGER.warning("[JELLYFIN] Subtitle lookup failed for item %s: %s", item_id, exc)
        raise HTTPException(status_code=503, detail="Subtitle index is temporarily unavailable")
    subtitles = sorted(
        (sub for sub in subtitles if sub.get("encoded") and sub.get("name")),
        key=lambda sub: (
            str(sub.get("lang_label") or "Unknown").casefold(),
            str(sub.get("name") or "").casefold(),
            int(sub.get("msg_id") or 0),
        ),
    )
    if index < 0 or index >= len(subtitles):
        raise HTTPException(status_code=404, detail="Subtitle track not found")

    selected = subtitles[index]
    actual_ext = subtitle_ext(str(selected.get("name") or "subtitle.srt"))
    requested_ext = "." + re.sub(r"[^a-z0-9]", "", subtitle_format.casefold())
    if requested_ext != actual_ext:
        raise HTTPException(status_code=404, detail="Subtitle format does not match the selected track")
    source_token, source_token_data = await _source_access()
    encoded = str(selected.get("encoded") or "")
    if not encoded:
        raise HTTPException(status_code=404, detail="Subtitle source is unavailable")
    # Reuse the repository's existing Telegram subtitle downloader. Its source
    # token remains server-side; the Android client presents only its scoped,
    # short-lived Jellyfin playback ticket to this route.
    return await subtitle_handler(
        token=source_token,
        id=encoded,
        name=str(selected.get("name") or f"subtitle{actual_ext}"),
        token_data=source_token_data,
    )


@router.get("/Videos/{item_id}/{media_source_id}/stream")
@router.head("/Videos/{item_id}/{media_source_id}/stream")
@router.get("/Videos/{item_id}/{media_source_id}/stream.{container}")
@router.head("/Videos/{item_id}/{media_source_id}/stream.{container}")
async def jellyfin_video_stream_with_source(
    item_id: str, media_source_id: str, request: Request, container: Optional[str] = None,
):
    return await _dispatch_jellyfin_stream(item_id, request, media_source_id)


@router.get("/Videos/{item_id}/stream")
@router.head("/Videos/{item_id}/stream")
@router.get("/Videos/{item_id}/stream.{container}")
@router.head("/Videos/{item_id}/stream.{container}")
async def jellyfin_video_stream(item_id: str, request: Request, container: Optional[str] = None):
    return await _dispatch_jellyfin_stream(item_id, request)


@router.get("/Items/{item_id}/File")
@router.head("/Items/{item_id}/File")
@router.get("/Items/{item_id}/Download")
@router.head("/Items/{item_id}/Download")
async def jellyfin_item_file(item_id: str, request: Request):
    return await _dispatch_jellyfin_stream(item_id, request)


@router.get("/Items/{item_id}/Images")
async def jellyfin_item_images(item_id: str, request: Request):
    await _session_for_request(request)
    _, doc, season, episode = await _resolve_item(item_id)
    image_infos = []
    for image_type in ("Primary", "Backdrop", "Logo"):
        image_url = _image_url(doc, season, episode, image_type)
        if not image_url:
            continue
        if str(image_url).startswith("/"):
            image_url = "https://image.tmdb.org/t/p/original" + str(image_url)
        image_infos.append({
            "ImageType": image_type,
            "ImageIndex": 0,
            "ImageTag": _image_tag(str(image_url)),
            "Path": str(image_url),
            "BlurHash": None,
            "Width": None,
            "Height": None,
            "Size": 0,
        })
    return image_infos


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
        payload, _, _, episode = await _resolve_item(item_id)
        if payload.get("collection") != "movie" and episode is None:
            raise HTTPException(status_code=400, detail="Playback progress is only valid for movies and episodes")
        position = max(0, int(body.get("PlaybackPositionTicks") or 0))
        await _tracking_collection("jellyfin_playback_state").update_one(
            {"user_id": session["user_id"], "item_id": item_id},
            {"$set": {
                "user_id": session["user_id"],
                "item_id": item_id,
                "position_ticks": position,
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
                "position_ticks": 0 if bool(body.get("Played", False) or body.get("PlayedToCompletion", False)) else int(body.get("PlaybackPositionTicks") or 0),
                "played": bool(body.get("Played", False) or body.get("PlayedToCompletion", False)),
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    return Response(status_code=204)


@router.post("/Sessions/Playing/Ping")
async def jellyfin_playing_ping(request: Request):
    await _session_for_request(request)
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
