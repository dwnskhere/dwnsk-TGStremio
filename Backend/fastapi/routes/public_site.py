"""Public, read-only Library and Plans pages.

Only media with public visibility (or legacy records without visibility) are
returned. API payloads are explicit safe projections; Telegram message IDs,
chat IDs, filenames, tokens, database indices, and private visibility metadata
never leave this module.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from Backend import db
from Backend.helper.metadata import resolve_cover_url
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

try:
    from Backend.helper.stream_formatter import parse_filename
except Exception:  # Keep the public catalogue usable if optional parser deps fail.
    parse_filename = None

router = APIRouter()
templates = Jinja2Templates(directory="Backend/fastapi/templates")

# V1 display allowances from the operator-provided current plan copy. The plan
# duration/price/currency itself always comes from tracking.sub_plans. These
# daily caps are deliberately a manual display mapping until the plan schema
# gains explicit entitlements in a later release.
_MANUAL_DAILY_LIMITS_GB = {30: 50, 90: 70, 180: 98, 365: 137}
_PLAN_LABELS = {30: "Starter", 90: "Basic", 180: "Pro", 365: "Premium"}
_PUBLIC_LIST_MAX_CANDIDATES = 30_000  # page<=500 and page_size<=60
_STATS_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_STATS_LOCK = asyncio.Lock()

_PUBLIC_VISIBILITY = {
    "$or": [
        {"visibility": "public"},
        {"visibility": {"$exists": False}},
    ]
}
_PUBLIC_CATALOG = {
    "$or": [
        {"exclusive_catalog_id": {"$exists": False}},
        {"exclusive_catalog_id": None},
    ]
}
_ACTIVE_MOVIE = {"telegram": {"$elemMatch": {"is_dead": {"$ne": True}}}}
_ACTIVE_TV = {
    "seasons": {
        "$elemMatch": {
            "episodes": {
                "$elemMatch": {
                    "telegram": {"$elemMatch": {"is_dead": {"$ne": True}}}
                }
            }
        }
    }
}

_LIST_PROJECTION = {
    "_id": 0,
    "tmdb_id": 1,
    "imdb_id": 1,
    "title": 1,
    "title_english": 1,
    "original_title": 1,
    "release_year": 1,
    "release_year_end": 1,
    "rating": 1,
    "genres": 1,
    "poster": 1,
    "backdrop": 1,
    "description": 1,
    "updated_on": 1,
    "is_anime": 1,
    "telegram.quality": 1,
    "telegram.name": 1,
    "telegram.is_dead": 1,
    "seasons.season_number": 1,
    "seasons.episodes.episode_number": 1,
    "seasons.episodes.indexed_on": 1,
    "seasons.episodes.telegram.quality": 1,
    "seasons.episodes.telegram.name": 1,
    "seasons.episodes.telegram.is_dead": 1,
}

_DETAIL_MOVIE_PROJECTION = {
    "_id": 0,
    "tmdb_id": 1, "title": 1, "title_english": 1, "original_title": 1,
    "release_year": 1, "release_year_end": 1, "rating": 1, "genres": 1,
    "poster": 1, "backdrop": 1, "logo": 1, "description": 1, "runtime": 1,
    "telegram.quality": 1, "telegram.size": 1, "telegram.name": 1,
    "telegram.is_dead": 1, "telegram.parts.part_number": 1,
}
_DETAIL_TV_PROJECTION = {
    "_id": 0,
    "tmdb_id": 1, "title": 1, "title_english": 1, "original_title": 1,
    "release_year": 1, "release_year_end": 1, "rating": 1, "genres": 1,
    "poster": 1, "backdrop": 1, "logo": 1, "description": 1, "runtime": 1,
    "seasons.season_number": 1, "seasons.episodes.episode_number": 1,
    "seasons.episodes.title": 1, "seasons.episodes.overview": 1,
    "seasons.episodes.episode_backdrop": 1, "seasons.episodes.released": 1,
    "seasons.episodes.indexed_on": 1,
    "seasons.episodes.telegram.quality": 1, "seasons.episodes.telegram.size": 1,
    "seasons.episodes.telegram.name": 1, "seasons.episodes.telegram.is_dead": 1,
    "seasons.episodes.telegram.parts.part_number": 1,
}


def _storage_items():
    items = []
    for key, value in (getattr(db, "dbs", {}) or {}).items():
        match = re.fullmatch(r"storage_(\d+)", str(key))
        if match and value is not None:
            items.append((int(match.group(1)), value))
    return sorted(items, key=lambda item: item[0])


def _query(*parts: dict) -> dict:
    valid = [part for part in parts if part]
    return valid[0] if len(valid) == 1 else {"$and": valid} if valid else {}


def _active_items(items) -> list[dict]:
    return [item for item in (items or []) if isinstance(item, dict) and item.get("is_dead") is not True]


def _quality_labels(items) -> list[str]:
    labels = {str(item.get("quality") or "").strip() for item in _active_items(items)}
    labels.discard("")

    def order(label: str):
        match = re.search(r"(\d{3,4})\s*p", label, re.I)
        if match:
            return (0, int(match.group(1)), label.casefold())
        if "4k" in label.casefold() or "2160" in label:
            return (0, 2160, label.casefold())
        return (1, 99999, label.casefold())

    return sorted(labels, key=order)


def _safe_timestamp(value: Any) -> float:
    """Convert a DB datetime/string to a sortable timestamp without leaking it."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if not isinstance(value, datetime):
        return 0.0
    try:
        return value.timestamp()
    except (OSError, OverflowError, ValueError):
        return 0.0


def _safe_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _candidate_limit(page: int, page_size: int) -> int:
    """Bound per-store candidates while allowing the entire current catalogue to paginate."""
    page = max(1, int(page))
    page_size = max(1, min(60, int(page_size)))
    return min(page * page_size, _PUBLIC_LIST_MAX_CANDIDATES)


def _public_plan_record(plan: dict) -> dict | None:
    """Explicit public projection of a subscription plan; never return DB metadata."""
    try:
        days = int(plan.get("days") or 0)
    except (TypeError, ValueError):
        return None
    price = _safe_number(plan.get("price"), default=-1.0)
    if days <= 0 or price < 0:
        return None
    currency = re.sub(r"[^A-Z]", "", str(plan.get("currency") or "INR").upper())[:3] or "INR"
    name = str(plan.get("name") or plan.get("title") or "").strip()[:80]
    if not name:
        name = _PLAN_LABELS.get(days, f"{days}-Day Plan")
    return {
        "name": name,
        "days": days,
        "price": price,
        "currency": currency,
        "daily_limit_gb": _MANUAL_DAILY_LIMITS_GB.get(days),
        "monthly_data": "Unlimited",
    }


def _parsed_name(name: str) -> dict:
    if not name or not parse_filename:
        return {}
    try:
        value = parse_filename(str(name))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _is_4k(items) -> bool:
    for item in _active_items(items):
        q = str(item.get("quality") or "").casefold()
        meta = _parsed_name(item.get("name") or "")
        res = str(meta.get("resolution") or "").casefold()
        if any(token in q for token in ("4k", "2160", "uhd", "4320")) or res in ("2160p", "4320p"):
            return True
    return False


def _is_recent_episode(episode: dict, cutoff: datetime) -> bool:
    value = episode.get("indexed_on")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value >= cutoff and bool(_active_items(episode.get("telegram")))


def _card(doc: dict, media_type: str) -> dict | None:
    has_4k = False
    if media_type == "movie":
        items = _active_items(doc.get("telegram"))
        if not items:
            return None
        labels = _quality_labels(items)
        file_count = len(labels)  # Q10-C: distinct quality labels per title.
        has_4k = _is_4k(items)
        is_new = False
        seasons_count = 0
        episode_count = 0
    else:
        seasons = doc.get("seasons") or []
        active_episodes = []
        new_episode_found = False
        cutoff = datetime.utcnow() - timedelta(days=14)
        file_count = 0
        labels_set = set()
        for season in seasons:
            for episode in (season.get("episodes") or []):
                active = _active_items(episode.get("telegram"))
                if not active:
                    continue
                active_episodes.append(episode)
                episode_labels = _quality_labels(active)
                file_count += len(episode_labels)  # distinct qualities per episode.
                labels_set.update(episode_labels)
                has_4k = has_4k or _is_4k(active)
                if _is_recent_episode(episode, cutoff):
                    new_episode_found = True
        if not active_episodes:
            return None
        labels = sorted(labels_set, key=lambda value: (
            0, int(re.search(r"\d+", value).group()) if re.search(r"\d+", value) else 99999, value.casefold()
        ))
        is_new = new_episode_found
        seasons_count = sum(
            1 for season in seasons
            if any(_active_items(ep.get("telegram")) for ep in (season.get("episodes") or []))
        )
        episode_count = len(active_episodes)

    poster = resolve_cover_url(doc.get("poster") or "")
    backdrop = resolve_cover_url(doc.get("backdrop") or "")
    return {
        "tmdb_id": doc.get("tmdb_id"),
        "media_type": media_type,
        "title": doc.get("title") or doc.get("title_english") or "Untitled",
        "year": doc.get("release_year"),
        "year_end": doc.get("release_year_end"),
        "rating": doc.get("rating"),
        "genres": list(doc.get("genres") or [])[:5],
        "poster": poster,
        "backdrop": backdrop,
        "quality_labels": labels,
        "file_count": file_count,
        "season_count": seasons_count,
        "episode_count": episode_count,
        "is_new": is_new,
        "is_anime": bool(doc.get("is_anime")),
        # Internal sort/filter fields are removed before returning API JSON.
        "_sort_ts": _safe_timestamp(doc.get("updated_on")),
        "_has_4k": has_4k,
    }


def _public_file_info(item: dict) -> dict:
    """Return parsed, non-identifying technical facts; never return item['name'] or IDs."""
    meta = _parsed_name(item.get("name") or "")
    video = [str(x) for x in (meta.get("visualTags") or [])]
    audio = [str(x) for x in (meta.get("audioTags") or [])]
    audio.extend(str(x) for x in (meta.get("audioChannels") or []))
    parts = item.get("parts") or []
    return {
        "quality": str(item.get("quality") or meta.get("resolution") or "Unknown"),
        "resolution": str(meta.get("resolution") or item.get("quality") or "Unknown"),
        "source": str(meta.get("quality") or "Not identified"),
        "codec": str(meta.get("encode") or "Not identified"),
        "video": " · ".join(dict.fromkeys(video)) or "Not identified",
        "audio": " · ".join(dict.fromkeys(audio)) or "Not identified",
        "languages": list(dict.fromkeys(str(x) for x in (meta.get("languages") or [])))[:8],
        "container": str(meta.get("container") or meta.get("extension") or "Not identified").upper()[:12],
        "size": str(item.get("size") or "Not available")[:40],
        "parts_count": len(parts) if parts else 1,
    }


def _safe_detail(doc: dict, media_type: str) -> dict:
    base = {
        "tmdb_id": doc.get("tmdb_id"),
        "media_type": media_type,
        "title": doc.get("title") or doc.get("title_english") or "Untitled",
        "original_title": doc.get("original_title") or doc.get("title") or "",
        "year": doc.get("release_year"),
        "year_end": doc.get("release_year_end"),
        "rating": doc.get("rating"),
        "genres": list(doc.get("genres") or [])[:8],
        "poster": resolve_cover_url(doc.get("poster") or ""),
        "backdrop": resolve_cover_url(doc.get("backdrop") or ""),
        "logo": resolve_cover_url(doc.get("logo") or ""),
        "overview": str(doc.get("description") or "")[:6000],
        "runtime": str(doc.get("runtime") or "")[:80],
    }
    if media_type == "movie":
        files = [_public_file_info(item) for item in _active_items(doc.get("telegram"))]
        base["files"] = files
        base["file_count"] = len(_quality_labels(doc.get("telegram")))
        return base

    seasons_out = []
    for season in sorted(doc.get("seasons") or [], key=lambda item: int(item.get("season_number") or 0)):
        eps_out = []
        for episode in sorted(season.get("episodes") or [], key=lambda item: int(item.get("episode_number") or 0)):
            active = _active_items(episode.get("telegram"))
            if not active:
                continue
            eps_out.append({
                "episode_number": episode.get("episode_number"),
                "title": str(episode.get("title") or "Episode")[:220],
                "overview": str(episode.get("overview") or "")[:2500],
                "released": str(episode.get("released") or "")[:40],
                "backdrop": resolve_cover_url(episode.get("episode_backdrop") or ""),
                "files": [_public_file_info(item) for item in active],
                "quality_labels": _quality_labels(active),
                "file_count": len(_quality_labels(active)),
            })
        if eps_out:
            seasons_out.append({"season_number": season.get("season_number"), "episodes": eps_out})
    base["seasons"] = seasons_out
    base["season_count"] = len(seasons_out)
    base["episode_count"] = sum(len(season["episodes"]) for season in seasons_out)
    return base


def _safe_telegram_link(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    username = raw.removeprefix("@").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
        return f"https://t.me/{username}"
    match = re.fullmatch(r"https://(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})/?", raw, re.I)
    if match:
        return f"https://t.me/{match.group(1)}"
    return ""


def _public_links() -> dict:
    settings = SettingsManager.current().to_dict()
    purchase = _safe_telegram_link(settings.get("public_bot_username"))
    if not purchase:
        try:
            from Backend.pyrofork.bot import StreamBot
            purchase = _safe_telegram_link(getattr(StreamBot, "username", ""))
        except Exception:
            purchase = ""
    support = _safe_telegram_link(settings.get("public_support_contact"))
    return {"purchase_url": purchase, "support_url": support}


async def _get_public_document(media_type: str, tmdb_id: int) -> dict | None:
    """Resolve the newest stored copy first, then enforce public visibility.

    Do not query for `visibility=public` while walking backwards through storage
    databases: if an older duplicate is public but a newer copy is restricted,
    filtering inside MongoDB could accidentally fall back to the older public copy.
    """
    collection = "movie" if media_type == "movie" else "tv"
    projection = _DETAIL_MOVIE_PROJECTION if media_type == "movie" else _DETAIL_TV_PROJECTION
    projection = {**projection, "visibility": 1, "exclusive_catalog_id": 1}
    for _, storage in reversed(_storage_items()):
        doc = await storage[collection].find_one({"tmdb_id": int(tmdb_id)}, projection)
        if not doc:
            continue
        if "visibility" in doc and doc.get("visibility") != "public":
            return None
        if doc.get("exclusive_catalog_id") is not None:
            return None
        if media_type == "movie":
            if not _active_items(doc.get("telegram")):
                return None
        else:
            has_active_episode = any(
                _active_items(episode.get("telegram"))
                for season in (doc.get("seasons") or [])
                for episode in (season.get("episodes") or [])
            )
            if not has_active_episode:
                return None
        return doc
    return None


def _search_clause(q: str) -> dict:
    q = (q or "").strip()[:80]
    if not q:
        return {}
    imdb_match = re.fullmatch(r"(tt\d{5,})", q, re.I)
    if imdb_match:
        return {"imdb_id": {"$regex": "^" + re.escape(imdb_match.group(1)), "$options": "i"}}
    if q.isdigit():
        return {"tmdb_id": int(q)}
    year_match = re.fullmatch(r"(.+?)\s*\(?((?:19|20|21)\d{2})\)?", q)
    year_clause = {}
    if year_match:
        q = year_match.group(1).strip()
        year_clause = {"release_year": int(year_match.group(2))}
    regex = {"$regex": re.escape(q), "$options": "i"}
    ors = [{field: regex} for field in ("title", "title_english", "original_title")]
    return _query({"$or": ors}, year_clause)


def _filter_clause(media_type: str, filter_name: str) -> dict:
    if filter_name == "4k":
        rx = {"$regex": r"(2160|4k|uhd|4320)", "$options": "i"}
        stream_match = {"$elemMatch": {"is_dead": {"$ne": True}, "$or": [{"quality": rx}, {"name": rx}]}}
        if media_type == "movie":
            return {"telegram": stream_match}
        return {"seasons": {"$elemMatch": {"episodes": {"$elemMatch": {"telegram": stream_match}}}}}
    if filter_name == "new":
        cutoff = datetime.utcnow() - timedelta(days=14)
        return {"seasons": {"$elemMatch": {"episodes": {"$elemMatch": {
            "indexed_on": {"$gte": cutoff},
            "telegram": {"$elemMatch": {"is_dead": {"$ne": True}}},
        }}}}}
    return {}


async def _load_list_page(
    collection: str, media_type: str, query: dict, page: int, page_size: int, sort: str, filter_name: str
) -> tuple[list[dict], int]:
    """Fetch enough top candidates from every storage DB for a global mixed sort.

    Pagination is applied once, after movie and TV candidates are merged, so page
    two and later pages do not accidentally repeat page one.
    """
    stores = _storage_items()
    if not stores:
        return [], 0
    counts = await asyncio.gather(*(store[collection].count_documents(query) for _, store in stores))
    total = sum(counts)
    if not total:
        return [], 0
    mongo_sort = [("updated_on", -1)]
    if sort == "title":
        mongo_sort = [("title", 1)]
    elif sort == "rating":
        mongo_sort = [("rating", -1)]
    wanted = _candidate_limit(page, page_size)
    per_store = await asyncio.gather(*(
        store[collection].find(query, _LIST_PROJECTION).sort(mongo_sort).limit(wanted).to_list(wanted)
        for _, store in stores
    ))
    docs = [doc for group in per_store for doc in group]
    cards = []
    for doc in docs:
        card = _card(doc, media_type)
        if card is None:
            continue
        if filter_name == "4k" and not card.get("_has_4k"):
            continue
        cards.append(card)
    return cards, total



async def _public_stats() -> dict:
    now = time.monotonic()
    if _STATS_CACHE["value"] is not None and now - _STATS_CACHE["at"] < 90:
        return _STATS_CACHE["value"]
    async with _STATS_LOCK:
        now = time.monotonic()
        if _STATS_CACHE["value"] is not None and now - _STATS_CACHE["at"] < 90:
            return _STATS_CACHE["value"]
        stats = {"movies": 0, "movie_files": 0, "tv_shows": 0, "episode_files": 0, "episodes": 0}
        for _, storage in _storage_items():
            movie_query = _query(_PUBLIC_VISIBILITY, _PUBLIC_CATALOG, _ACTIVE_MOVIE)
            tv_query = _query(_PUBLIC_VISIBILITY, _PUBLIC_CATALOG, _ACTIVE_TV)
            movies = storage["movie"].find(movie_query, {"telegram.quality": 1, "telegram.is_dead": 1})
            async for doc in movies:
                labels = _quality_labels(doc.get("telegram"))
                if labels:
                    stats["movies"] += 1
                    stats["movie_files"] += len(labels)
            shows = storage["tv"].find(tv_query, {
                "seasons.episodes.telegram.quality": 1,
                "seasons.episodes.telegram.is_dead": 1,
            })
            async for doc in shows:
                has_active = False
                for season in doc.get("seasons") or []:
                    for episode in season.get("episodes") or []:
                        labels = _quality_labels(episode.get("telegram"))
                        if labels:
                            has_active = True
                            stats["episodes"] += 1
                            stats["episode_files"] += len(labels)
                if has_active:
                    stats["tv_shows"] += 1
        _STATS_CACHE.update({"at": time.monotonic(), "value": stats})
        return stats


@router.get("/library", response_class=HTMLResponse)
async def public_library_alias(request: Request):
    return await public_library_page(request)


async def public_library_page(request: Request):
    return templates.TemplateResponse("public_library.html", {
        "request": request,
        **_public_links(),
    })


@router.get("/plans", response_class=HTMLResponse)
async def public_plans_page(request: Request):
    try:
        plans = await db.get_subscription_plans()
    except Exception as exc:
        LOGGER.error(f"Public plans load failed: {exc}")
        plans = []
    clean_plans = [public_plan for plan in plans if (public_plan := _public_plan_record(plan))]
    return templates.TemplateResponse("public_plans.html", {
        "request": request,
        "plans": clean_plans,
        **_public_links(),
    })


@router.get("/library/movie/{tmdb_id}", response_class=HTMLResponse)
async def public_movie_page(request: Request, tmdb_id: int):
    doc = await _get_public_document("movie", tmdb_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Title not found")
    detail = _safe_detail(doc, "movie")
    return templates.TemplateResponse("public_detail.html", {
        "request": request, "media": detail, "is_movie": True, **_public_links(),
    })


@router.get("/library/tv/{tmdb_id}", response_class=HTMLResponse)
async def public_tv_page(request: Request, tmdb_id: int):
    doc = await _get_public_document("tv", tmdb_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Title not found")
    detail = _safe_detail(doc, "tv")
    return templates.TemplateResponse("public_detail.html", {
        "request": request, "media": detail, "is_movie": False, **_public_links(),
    })


@router.get("/api/public/library")
async def public_library_api(
    q: str = Query("", max_length=80),
    media_type: str = Query("all", pattern="^(all|movie|tv)$"),
    filter_by: str = Query("all", alias="filter", pattern="^(all|4k|new)$"),
    sort: str = Query("recent", pattern="^(recent|title|rating)$"),
    page: int = Query(1, ge=1, le=500),
    page_size: int = Query(24, ge=1, le=60),
):
    try:
        public_query = _query(_PUBLIC_VISIBILITY, _PUBLIC_CATALOG)
        search = _search_clause(q)
        results = []
        total = 0
        types = ("movie", "tv") if media_type == "all" else (media_type,)
        for kind in types:
            active = _ACTIVE_MOVIE if kind == "movie" else _ACTIVE_TV
            filter_part = _filter_clause(kind, filter_by)
            query = _query(public_query, active, search, filter_part)
            cards, count = await _load_list_page(kind, kind, query, page, page_size, sort, filter_by)
            results.extend(cards)
            total += count
        if sort == "title":
            results.sort(key=lambda x: (str(x.get("title") or "").casefold(), x.get("media_type") or ""))
        elif sort == "rating":
            results.sort(key=lambda x: (_safe_number(x.get("rating")), str(x.get("title") or "").casefold()), reverse=True)
        else:
            results.sort(key=lambda x: (x.get("_sort_ts", 0), str(x.get("title") or "").casefold()), reverse=True)
        page_results = results[(page - 1) * page_size:page * page_size]
        for item in page_results:
            item.pop("_sort_ts", None)
            item.pop("_has_4k", None)
        stats = await _public_stats()
        return JSONResponse({
            "items": page_results,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
            "stats": stats,
        }, headers={"Cache-Control": "public, max-age=20"})
    except Exception as exc:
        LOGGER.error(f"Public library API failed: {exc}")
        raise HTTPException(status_code=500, detail="Could not load the public library")


@router.get("/api/public/plans")
async def public_plans_api():
    try:
        plans = await db.get_subscription_plans()
        cleaned = [public_plan for plan in plans if (public_plan := _public_plan_record(plan))]
        return JSONResponse({"plans": cleaned}, headers={"Cache-Control": "public, max-age=60"})
    except Exception as exc:
        LOGGER.error(f"Public plans API failed: {exc}")
        raise HTTPException(status_code=500, detail="Could not load subscription plans")


@router.get("/api/public/library/{media_type}/{tmdb_id}")
async def public_detail_api(media_type: str, tmdb_id: int):
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=404, detail="Title not found")
    doc = await _get_public_document(media_type, tmdb_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Title not found")
    return JSONResponse(_safe_detail(doc, media_type), headers={"Cache-Control": "public, max-age=30"})
