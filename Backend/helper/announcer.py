"""Telegram channel announcements for newly indexed media.

Announcements are keyed per movie, episode, or completed season. The canonical
post is kept current; a newly-added quality also produces a fresh, marked alert
post so channel subscribers can notice the update. Posts are Telegram Rich
Messages (HTML + poster slideshow) sent through the Bot API.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
from urllib.parse import quote
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageDeleteForbidden, MessageIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from Backend import db
from Backend.config import Telegram
from Backend.helper.metadata.common import format_tmdb_image
from Backend.helper.metadata.providers import tmdb as tmdb_provider
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, get_streambot_url


_BOT_API_BASE = "https://api.telegram.org/bot{}"
_TELEGRAPH_API_BASE = "https://api.telegra.ph"
_TELEGRAPH_ACCOUNT_LOCK = asyncio.Lock()
_SEASON_CACHE_TTL = 60 * 60
_SEASON_NEGATIVE_CACHE_TTL = 5 * 60
_IMAGE_CACHE_TTL = 6 * 60 * 60
_RECONCILE_INTERVAL_SECONDS = 60 * 60
_SEASON_METADATA_CACHE: dict[tuple[int, int], tuple[float, dict | None]] = {}
_SEASON_PROGRESS_CACHE: dict[tuple[int, int], tuple[float, dict | None]] = {}
_TMDB_IMAGE_CACHE: dict[tuple, tuple[float, dict]] = {}
_ANNOUNCEMENT_LOCKS: dict[str, asyncio.Lock] = {}
_ANNOUNCEMENT_RECONCILER_TASK: asyncio.Task | None = None


class _BotAPIError(RuntimeError):
    def __init__(self, method: str, code: int, description: str, parameters: dict | None = None):
        self.method = method
        self.code = code
        self.description = description
        self.parameters = parameters or {}
        super().__init__(f"Telegram Bot API {method} failed ({code}): {description}")


# Accept either a numeric channel id (-100...) or an @username.
def _resolve_chat(value: str):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _episode_key(tmdb_id, season_number: int, episode_number: int) -> str:
    return f"tv:{tmdb_id}:episode:{season_number:02d}:{episode_number:03d}"


def _season_key(tmdb_id, season_number: int) -> str:
    return f"tv:{tmdb_id}:season:{season_number:02d}"


def _quality_sort_key(value: str):
    text = str(value or "").strip().lower()
    if "8k" in text:
        return (4320, text)
    if "4k" in text or "2160" in text:
        return (2160, text)
    match = re.search(r"(\d{3,4})\s*p?", text)
    return (int(match.group(1)) if match else 0, text)


def _unique_qualities(items) -> list[str]:
    values = set()
    for item in items or []:
        item = item or {}
        if item.get("is_dead"):
            continue
        quality = str(item.get("quality") or "").strip()
        if quality:
            values.add(quality)
    return sorted(values, key=_quality_sort_key)


def _normalise_quality_names(values) -> list[str]:
    """Normalize quality labels from either DB objects or plain string lists."""
    if isinstance(values, str):
        values = [values]
    items = []
    for value in values or []:
        if isinstance(value, dict):
            items.append(value)
        elif value:
            items.append({"quality": str(value)})
    return _unique_qualities(items)


def _new_quality_labels(current, previous) -> list[str]:
    """Return only quality labels newly present in the current announcement."""
    previous_set = set(_normalise_quality_names(previous))
    return [
        quality for quality in _normalise_quality_names(current)
        if quality not in previous_set
    ]


def _released_regular_episode_numbers(metadata_episodes, expected_count: int, today=None) -> set[int] | None:
    """Return the metadata-defined regular episodes only when the season has aired.

    Missing air dates, incomplete metadata lists, and future episode dates are
    deliberately treated as "not complete yet" rather than guessed.
    """
    expected_count = _as_int(expected_count, 0)
    episodes = list(metadata_episodes or [])
    if not episodes:
        LOGGER.debug("TMDb season completion check: no episode records returned")
        return None
    if expected_count > 0 and len(episodes) < expected_count:
        LOGGER.debug(
            f"TMDb season metadata incomplete: returned {len(episodes)} of {expected_count} episodes"
        )
        return None

    today = today or datetime.now(timezone.utc).date()
    episode_numbers = set()
    for item in episodes:
        number = _as_int(item.get("episode_number"), 0)
        episode_type = str(item.get("episode_type") or "standard").lower()
        if number <= 0 or episode_type == "special":
            continue

        air_date = str(item.get("air_date") or "").strip()
        if not air_date:
            LOGGER.debug(
                f"TMDb season completion check: episode {number} has no air_date; waiting for metadata"
            )
            return None
        try:
            parsed_date = date.fromisoformat(air_date[:10])
        except ValueError:
            LOGGER.debug(
                f"TMDb season completion check: episode {number} has invalid air_date={air_date!r}"
            )
            return None
        if parsed_date > today:
            LOGGER.debug(
                f"TMDb season completion check: episode {number} has future air_date={air_date}"
            )
            return None
        episode_numbers.add(number)

    return episode_numbers or None


def _movie_qualities(doc: dict | None) -> list[str]:
    return _unique_qualities((doc or {}).get("telegram") or [])


def _find_season(doc: dict | None, season_number: int) -> dict | None:
    for season in (doc or {}).get("seasons") or []:
        if _as_int(season.get("season_number"), -1) == season_number:
            return season
    return None


def _find_episode(doc: dict | None, season_number: int, episode_number: int) -> dict | None:
    season = _find_season(doc, season_number)
    if not season:
        return None
    for episode in season.get("episodes") or []:
        if _as_int(episode.get("episode_number"), -1) == episode_number:
            return episode
    return None


def _episode_qualities(episode: dict | None) -> list[str]:
    return _unique_qualities((episode or {}).get("telegram") or [])


def _season_qualities(season: dict | None, episode_numbers: set[int] | None = None) -> list[str]:
    quality_items = []
    for episode in (season or {}).get("episodes") or []:
        number = _as_int(episode.get("episode_number"), -1)
        # Exclude season-zero specials and the synthetic combined-pack entries.
        if number <= 0 or number >= 1000:
            continue
        if episode_numbers is not None and number not in episode_numbers:
            continue
        quality_items.extend(episode.get("telegram") or [])
    return _unique_qualities(quality_items)


def _base_info_from_doc(doc: dict, media_type: str) -> dict:
    return {
        "tmdb_id": doc.get("tmdb_id"),
        "imdb_id": doc.get("imdb_id"),
        "media_type": media_type,
        "title": doc.get("title") or "Unknown",
        "original_title": doc.get("original_title") or doc.get("title") or "Unknown",
        "year": doc.get("release_year") or 0,
        "rate": doc.get("rating") or 0,
        "description": doc.get("description") or "",
        "poster": doc.get("poster") or "",
        "backdrop": doc.get("backdrop") or "",
        "genres": doc.get("genres") or [],
        "runtime": str(doc.get("runtime") or ""),
    }


def _build_caption(info: dict) -> str:
    """Build the clean two-column TMDb announcement requested for the channel."""
    kind = info.get("announcement_kind") or "movie"
    title = str(info.get("title") or "Unknown").strip()
    original_name = str(info.get("original_title") or title).strip() or title
    aka = str(info.get("aka") or "N/A").strip() or "N/A"
    runtime = str(info.get("runtime") or "N/A").strip() or "N/A"
    genres = ", ".join(str(value) for value in (info.get("genres") or [])[:6] if value) or "N/A"

    tmdb_id = _as_int(info.get("tmdb_id"), 0)
    if kind == "episode":
        season_number = _as_int(info.get("season_number"), 0)
        episode_number = _as_int(info.get("episode_number"), 0)
        episode_title = str(info.get("episode_title") or "").strip()
        episode_code = f"S{season_number:02d}E{episode_number:02d}"
        episode_suffix = f" - {episode_title}" if episode_title and episode_title.casefold() != episode_code.casefold() else ""
        header = f"📺 <b>{html.escape(title, quote=False)} – {episode_code}{html.escape(episode_suffix, quote=False)}</b>"
        aka = f"{title} {episode_code}{episode_suffix}"
        media_label = "TV Episode"
        description = info.get("episode_overview") or info.get("description") or ""
        tmdb_url = f"https://www.themoviedb.org/tv/{tmdb_id}/season/{season_number}/episode/{episode_number}" if tmdb_id else ""
    elif kind == "season":
        season_number = _as_int(info.get("season_number"), 0)
        season_name = str(info.get("season_name") or f"Season {season_number}")
        header = f"📺 <b>{html.escape(title, quote=False)} — {html.escape(season_name, quote=False)}</b>"
        media_label = "Complete Season"
        description = info.get("season_overview") or info.get("description") or ""
        tmdb_url = f"https://www.themoviedb.org/tv/{tmdb_id}/season/{season_number}" if tmdb_id else ""
    elif info.get("media_type") == "tv":
        header = f"📺 <b>{html.escape(title, quote=False)}</b>"
        media_label = "TV Series"
        description = info.get("description") or ""
        tmdb_url = f"https://www.themoviedb.org/tv/{tmdb_id}" if tmdb_id else ""
    else:
        year = _as_int(info.get("year"), 0)
        year_suffix = f" ({year})" if year else ""
        header = f"🎬 <b>{html.escape(title, quote=False)}{year_suffix}</b>"
        media_label = "Movie"
        description = info.get("description") or ""
        tmdb_url = f"https://www.themoviedb.org/movie/{tmdb_id}" if tmdb_id else ""

    def cell(label: str, value: str) -> str:
        # Keep the field name visually distinct from its value, matching the
        # two-column sample layout supplied for channel announcements.
        return (
            f"<td><b>{html.escape(label, quote=False)}</b><br/>"
            f"{html.escape(str(value or 'N/A'), quote=False)}</td>"
        )

    tmdb_cell = (
        f'<td><b>TMDB Link</b><br/><a href="{html.escape(tmdb_url, quote=True)}">Open TMDB</a></td>'
        if tmdb_url else cell("TMDB Link", "N/A")
    )
    table = "\n".join([
        "<table bordered striped compact>",
        "<tr>" + cell("Original Name", original_name) + cell("AKA", aka) + "</tr>",
        "<tr>" + cell("Type", media_label) + cell("Genres", genres) + "</tr>",
        "<tr>" + cell("Runtime", runtime) + tmdb_cell + "</tr>",
        "</table>",
    ])

    parts = [header]
    newly_added = _normalise_quality_names(info.get("newly_added_qualities"))
    if newly_added:
        added_text = html.escape(" | ".join(newly_added), quote=False)
        parts.extend(["", f"🆕 <b>NEW QUALITY ADDED:</b> <b>{added_text}</b>"])
    parts.extend(["", table])
    description = str(description or "").strip()
    if description:
        parts.extend([
            "",
            f"<details><summary><b>Overview (Tap to Expand)</b></summary><i>{html.escape(description, quote=False)}</i></details>",
        ])

    qualities = info.get("qualities") or ([info.get("quality")] if info.get("quality") else [])
    qualities = [str(q).strip() for q in qualities if str(q or "").strip()]
    if qualities:
        parts.extend(["", "<hr/>", f"📶 <b>Quality:</b> {html.escape(' | '.join(qualities), quote=False)}"])
    return "\n".join(parts)


def _build_classic_caption(info: dict) -> str:
    """Telegram HTML fallback for Bot API deployments without Rich Messages."""
    kind = info.get("announcement_kind") or "movie"
    title = str(info.get("title") or "Unknown")
    if kind == "episode":
        s_num = _as_int(info.get("season_number"), 0)
        e_num = _as_int(info.get("episode_number"), 0)
        episode_title = str(info.get("episode_title") or "").strip()
        code = f"S{s_num:02d}E{e_num:02d}"
        heading = f"📺 <b>{html.escape(title)} — {code}{(' - ' + html.escape(episode_title)) if episode_title and episode_title.casefold() != code.casefold() else ''}</b>"
        description = info.get("episode_overview") or info.get("description") or ""
    elif kind == "season":
        heading = f"📺 <b>{html.escape(title)} — Season {_as_int(info.get('season_number'), 0)}</b>"
        description = info.get("season_overview") or info.get("description") or ""
    elif info.get("media_type") == "tv":
        heading = f"📺 <b>{html.escape(title)}</b>"
        description = info.get("description") or ""
    else:
        year = _as_int(info.get("year"), 0)
        heading = f"🎬 <b>{html.escape(title)}{f' ({year})' if year else ''}</b>"
        description = info.get("description") or ""

    lines = [heading]
    newly_added = _normalise_quality_names(info.get("newly_added_qualities"))
    if newly_added:
        lines.extend([
            "",
            f"🆕 <b>NEW QUALITY ADDED:</b> <b>{html.escape(' | '.join(newly_added))}</b>",
        ])
    fields = [
        ("Original Name", info.get("original_title") or title),
        ("AKA", info.get("aka") or "N/A"),
        ("Type", "TV Episode" if kind == "episode" else "Complete Season" if kind == "season" else "TV Series" if info.get("media_type") == "tv" else "Movie"),
        ("Genres", ", ".join(info.get("genres") or []) or "N/A"),
        ("Runtime", info.get("runtime") or "N/A"),
    ]
    for label, value in fields:
        lines.append(f"<b>{label}:</b> {html.escape(str(value))}")
    tmdb_id = _as_int(info.get("tmdb_id"), 0)
    if tmdb_id:
        if kind == "episode":
            url = f"https://www.themoviedb.org/tv/{tmdb_id}/season/{_as_int(info.get('season_number'), 0)}/episode/{_as_int(info.get('episode_number'), 0)}"
        elif kind == "season":
            url = f"https://www.themoviedb.org/tv/{tmdb_id}/season/{_as_int(info.get('season_number'), 0)}"
        elif info.get("media_type") == "tv":
            url = f"https://www.themoviedb.org/tv/{tmdb_id}"
        else:
            url = f"https://www.themoviedb.org/movie/{tmdb_id}"
        lines.append(f'<b>TMDB Link:</b> <a href="{url}">Open TMDB</a>')
    if description:
        lines.extend(["", "<b>Overview:</b>", html.escape(str(description))])
    qualities = info.get("qualities") or ([info.get("quality")] if info.get("quality") else [])
    qualities = [str(q).strip() for q in qualities if str(q or "").strip()]
    if qualities:
        lines.extend(["", f"📶 <b>Quality:</b> {html.escape(' | '.join(qualities))}"])
    caption = "\n".join(lines)
    return caption[:1000] + ("…" if len(caption) > 1000 else "")


def _build_markup_data(info: dict) -> dict | None:
    rows = []
    base = str(SettingsManager.current().base_url or "").rstrip("/")
    imdb_id = str(info.get("imdb_id") or "").strip()
    stremio_type = "series" if info.get("media_type") == "tv" else "movie"
    if base and imdb_id:
        rows.append([
            {"text": "▶️ Stremio", "url": f"{base}/open/stremio/{stremio_type}/{imdb_id}"},
            {"text": "📱 Nuvio", "url": f"{base}/open/nuvio/{stremio_type}/{imdb_id}"},
        ])
    bot_url = get_streambot_url()
    if bot_url and bot_url != "https://t.me/":
        rows.append([{"text": "🤖 Get Addon", "url": bot_url}])
    return {"inline_keyboard": rows} if rows else None


def _build_markup(info: dict):
    """Pyrogram InlineKeyboardMarkup fallback for classic messages."""
    data = _build_markup_data(info)
    if not data:
        return None
    rows = [
        [InlineKeyboardButton(text=button["text"], url=button["url"]) for button in row]
        for row in data["inline_keyboard"]
    ]
    return InlineKeyboardMarkup(rows)


async def _bot_api_request(method: str, payload: dict) -> Any:
    token = str(Telegram.BOT_TOKEN or "").strip()
    if not token:
        raise _BotAPIError(method, 401, "BOT_TOKEN is not configured")

    url = f"{_BOT_API_BASE.format(token)}/{method}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0)) as client:
        for attempt in range(2):
            response = await client.post(url, json=payload)
            try:
                body = response.json()
            except ValueError:
                body = {}
            if body.get("ok"):
                return body.get("result")

            code = _as_int(body.get("error_code"), response.status_code)
            description = str(body.get("description") or response.text or "Unknown Bot API error")
            parameters = body.get("parameters") or {}
            if code == 429 and attempt == 0:
                retry_after = _as_int(parameters.get("retry_after"), 1)
                await asyncio.sleep(max(1, min(retry_after, 60)))
                continue
            raise _BotAPIError(method, code, description, parameters)
    raise _BotAPIError(method, 500, "Request failed after retry")


async def _fetch_tmdb_images(endpoint: str, cache_key: tuple) -> dict:
    """Fetch a TMDb image endpoint, retaining a short process-local cache."""
    cached = _TMDB_IMAGE_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < _IMAGE_CACHE_TTL:
        return cached[1]

    api_key = tmdb_provider.tmdb_api_key()
    if not api_key:
        return {}
    url = f"https://api.themoviedb.org/3/{endpoint.lstrip('/')}"
    try:
        async with tmdb_provider.API_SEMAPHORE:
            async with httpx.AsyncClient(timeout=httpx.Timeout(18.0, connect=8.0)) as client:
                response = await client.get(url, params={
                    "api_key": api_key,
                    "include_image_language": "en-US,null",
                })
        if response.status_code != 200:
            LOGGER.debug(f"TMDb images endpoint returned {response.status_code} for {endpoint}")
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            return {}
        _TMDB_IMAGE_CACHE[cache_key] = (time.monotonic(), payload)
        return payload
    except Exception as e:
        LOGGER.debug(f"TMDb images fetch failed for {endpoint}: {type(e).__name__}")
        return {}


def _image_language_rank(item: dict) -> int:
    language = item.get("iso_639_1")
    return 0 if language == "en" else 1 if language in (None, "") else 2


def _landscape_image_url(item: dict) -> str:
    path = item.get("file_path")
    if not path:
        return ""
    try:
        width = float(item.get("width") or 0)
        height = float(item.get("height") or 0)
        ratio = float(item.get("aspect_ratio") or (width / height if height else 0))
    except (TypeError, ValueError, ZeroDivisionError):
        ratio = 0
    # TMDb stills/backdrops should be horizontal; don't let a portrait poster
    # enter one of the four landscape slots.
    if ratio < 1.15:
        return ""
    return format_tmdb_image(path, "original")


def _rank_image_items(items: list[dict]) -> list[dict]:
    def score(item):
        return (
            _image_language_rank(item),
            -float(item.get("vote_average") or 0),
            -int(item.get("vote_count") or 0),
            -int(item.get("width") or 0),
        )
    return sorted((item for item in (items or []) if isinstance(item, dict)), key=score)


def _official_portrait_poster(info: dict, image_data: dict, preferred_path: str = "") -> str:
    # Use only TMDb poster_path assets. For a consolidated season, prefer its
    # season poster; otherwise use the canonical movie/series poster_path.
    canonical_path = preferred_path or info.get("tmdb_poster_path") or ""
    primary = format_tmdb_image(canonical_path, "w500") if canonical_path else ""
    if primary:
        return primary

    for item in _rank_image_items(image_data.get("posters") or []):
        try:
            width = float(item.get("width") or 0)
            height = float(item.get("height") or 0)
            ratio = float(item.get("aspect_ratio") or (width / height if height else 0))
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = 0
        if ratio and ratio < 1.0 and item.get("file_path"):
            return format_tmdb_image(item["file_path"], "w500")
    return ""


async def _get_posters(info: dict, season_metadata: dict | None = None) -> list[str]:
    """Return one portrait poster + up to four horizontal assets for titles/seasons,
    or up to five horizontal episode stills for an individual episode.
    """
    tmdb_id = _as_int(info.get("tmdb_id"))
    media_type = str(info.get("media_type") or "tv")
    kind = info.get("announcement_kind") or "movie"
    if not tmdb_id:
        # These redesigned announcement galleries are explicitly TMDb-backed.
        # Do not silently fall back to unrelated provider artwork.
        return []

    if kind == "episode":
        season_number = _as_int(info.get("season_number"), 0)
        episode_number = _as_int(info.get("episode_number"), 0)
        endpoint = f"tv/{tmdb_id}/season/{season_number}/episode/{episode_number}/images"
        image_data = await _fetch_tmdb_images(endpoint, ("episode", tmdb_id, season_number, episode_number))
        stills = _rank_image_items(image_data.get("stills") or [])
        result = []
        for item in stills:
            url = _landscape_image_url(item)
            if url and url not in result:
                result.append(url)
            if len(result) >= 5:
                break
        # Do not fall back to generic show posters/backdrops here: this slot is
        # reserved for episode stills. Legacy providers sometimes populate an
        # episode thumbnail with a portrait poster, and its URL alone cannot
        # prove the orientation. An empty slideshow is preferable to violating
        # the landscape-only requirement.
        return result[:5]

    endpoint_type = "movie" if media_type == "movie" else "tv"
    image_data = await _fetch_tmdb_images(
        f"{endpoint_type}/{tmdb_id}/images", (endpoint_type, tmdb_id)
    )
    preferred_poster_path = ""
    if kind == "season":
        preferred_poster_path = (
            (season_metadata or {}).get("poster_path")
            or info.get("season_poster_path")
            or ""
        )
    poster = _official_portrait_poster(info, image_data, preferred_poster_path)

    backdrops = _rank_image_items(image_data.get("backdrops") or [])
    # TMDb's canonical backdrop_path is a landscape backdrop. Avoid using
    # arbitrary legacy `backdrop` values, which may be a portrait fallback.
    primary_backdrop = (
        format_tmdb_image(info.get("tmdb_backdrop_path"), "original")
        if info.get("tmdb_backdrop_path") else ""
    )
    result = [poster] if poster else []
    landscape_urls = []
    for item in backdrops:
        url = _landscape_image_url(item)
        if url and url not in landscape_urls:
            landscape_urls.append(url)
        if len(landscape_urls) >= 4:
            break
    if primary_backdrop and primary_backdrop not in landscape_urls:
        landscape_urls.append(primary_backdrop)
    result.extend(landscape_urls[:4])
    return result[:5]


def _build_rich_message(info: dict, poster_urls: list[str]) -> dict:
    media = []
    image_markup = []
    for index, url in enumerate(poster_urls, start=1):
        media_id = f"poster_{index}"
        media.append({
            "id": media_id,
            "media": {"type": "photo", "media": url},
        })
        image_markup.append(f'<img src="tg://photo?id={media_id}"/>')

    html_body = _build_caption(info)
    if image_markup:
        html_body = f"<tg-slideshow>{''.join(image_markup)}</tg-slideshow>\n\n{html_body}"
    return {"html": html_body, "media": media} if media else {"html": html_body}


async def _send_classic_fallback(chat, info: dict):
    caption = _build_classic_caption(info)
    markup = _build_markup(info)
    poster = str(info.get("poster") or "").strip()
    if poster:
        try:
            return await StreamBot.send_photo(
                chat, poster, caption=caption, parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except FloodWait:
            raise
        except Exception as e:
            LOGGER.warning(f"Classic announcement photo failed; sending text instead: {e}")
    return await StreamBot.send_message(
        chat, caption, parse_mode=ParseMode.HTML, reply_markup=markup,
        disable_web_page_preview=True,
    )


async def _send_announcement(chat, info: dict, poster_urls: list[str]):
    payload = {
        "chat_id": chat,
        "rich_message": _build_rich_message(info, poster_urls),
    }
    markup = _build_markup_data(info)
    if markup:
        payload["reply_markup"] = markup
    try:
        sent = await _bot_api_request("sendRichMessage", payload)
        return sent.get("message_id") if isinstance(sent, dict) else None
    except _BotAPIError as e:
        LOGGER.warning(f"Rich announcement unavailable; using classic Telegram message: {e}")
    except httpx.RequestError as e:
        # A timeout can occur after Telegram accepted the send; do not risk
        # emitting a second post blindly. The next indexing event can retry.
        LOGGER.error(f"Rich announcement request outcome is unknown ({type(e).__name__}).")
        return None

    sent = await _send_classic_fallback(chat, info)
    return getattr(sent, "id", None)


async def _edit_announcement(chat, message_id: int, info: dict, poster_urls: list[str]) -> bool:
    payload = {
        "chat_id": chat,
        "message_id": int(message_id),
        "rich_message": _build_rich_message(info, poster_urls),
    }
    markup = _build_markup_data(info)
    if markup:
        payload["reply_markup"] = markup
    try:
        await _bot_api_request("editMessageText", payload)
        return True
    except _BotAPIError as e:
        if "message is not modified" in e.description.lower():
            return True
        LOGGER.warning(f"Could not edit announcement {message_id}; replacing it: {e}")
        return False
    except httpx.RequestError as e:
        # The edit may have reached Telegram; keep the known message id and
        # retry on a later metadata/quality event rather than sending a duplicate.
        LOGGER.error(
            f"Announcement edit outcome is unknown for message {message_id} ({type(e).__name__})."
        )
        return True


async def _delete_telegram_message(chat_id, message_id) -> bool:
    if not chat_id or not message_id:
        return True
    try:
        deleted = await _bot_api_request("deleteMessage", {
            "chat_id": chat_id,
            "message_id": int(message_id),
        })
        if deleted is True:
            return True
        LOGGER.warning(f"Telegram did not confirm deletion of announcement {message_id}; trying Pyrogram")
        await StreamBot.delete_messages(chat_id, int(message_id))
        return True
    except _BotAPIError as e:
        # Not-found means the message is already gone. Other errors can be
        # permission failures; try Pyrogram before giving up.
        text = e.description.lower()
        if "message to delete not found" in text or "message_id_invalid" in text:
            return True
        try:
            await StreamBot.delete_messages(chat_id, int(message_id))
            return True
        except (MessageDeleteForbidden, MessageIdInvalid) as pyro_error:
            LOGGER.warning(f"Could not delete announcement {message_id}: {pyro_error}")
        except FloodWait as pyro_error:
            LOGGER.warning(f"FloodWait deleting announcement {message_id}: {pyro_error.value}s")
        except Exception as pyro_error:
            LOGGER.warning(f"Pyrogram delete fallback failed for {message_id}: {pyro_error}")
        return False
    except httpx.RequestError as e:
        LOGGER.warning(
            f"Bot API delete request failed for message {message_id} ({type(e).__name__})."
        )
        try:
            await StreamBot.delete_messages(chat_id, int(message_id))
            return True
        except Exception as pyro_error:
            LOGGER.warning(f"Pyrogram delete fallback failed for {message_id}: {pyro_error}")
            return False
    except FloodWait as e:
        LOGGER.warning(f"FloodWait deleting announcement {message_id}: {e.value}s")
        return False
    except Exception as e:
        LOGGER.warning(f"Failed to delete announcement message {message_id}: {e}")
        return False


async def _enrich_announcement_info(info: dict) -> dict:
    """Fill original name, AKA and runtime using the relevant TMDb records."""
    result = dict(info)
    kind = result.get("announcement_kind") or "movie"
    tmdb_id = _as_int(result.get("tmdb_id"), 0)
    media_type = "movie" if result.get("media_type") == "movie" else "tv"
    if not tmdb_id:
        return result

    jobs = [tmdb_provider.details(media_type, tmdb_id), tmdb_provider.alternative_titles(media_type, tmdb_id)]
    if kind == "episode":
        jobs.append(tmdb_provider.episode_details(
            tmdb_id,
            _as_int(result.get("season_number"), 0),
            _as_int(result.get("episode_number"), 0),
        ))
    fetched = await asyncio.gather(*jobs, return_exceptions=True)
    details = fetched[0] if fetched and not isinstance(fetched[0], Exception) else None
    aliases = fetched[1] if len(fetched) > 1 and not isinstance(fetched[1], Exception) else []
    episode_details = fetched[2] if len(fetched) > 2 and not isinstance(fetched[2], Exception) else None

    if details:
        original = (
            getattr(details, "original_title", None)
            if media_type == "movie"
            else getattr(details, "original_name", None)
        )
        if original:
            result["original_title"] = str(original)

        if media_type == "movie":
            runtime = _as_int(getattr(details, "runtime", None), 0)
            if runtime > 0:
                result["runtime"] = tmdb_provider.format_runtime(runtime) or f"{runtime} min"
        else:
            runtimes = sorted({
                _as_int(value, 0)
                for value in (getattr(details, "episode_run_time", None) or [])
                if _as_int(value, 0) > 0
            })
            if runtimes and kind != "episode":
                if len(runtimes) == 1:
                    result["runtime"] = f"{runtimes[0]} min per episode"
                else:
                    result["runtime"] = f"{runtimes[0]}–{runtimes[-1]} min per episode"

    if details:
        # Use canonical TMDb image paths for the title-level poster/backdrop,
        # rather than relying on an image inherited from another metadata source.
        poster_path = getattr(details, "poster_path", None)
        backdrop_path = getattr(details, "backdrop_path", None)
        if poster_path:
            result["tmdb_poster_path"] = str(poster_path)
        if backdrop_path:
            result["tmdb_backdrop_path"] = str(backdrop_path)

    if kind == "episode":
        if episode_details:
            episode_title = getattr(episode_details, "name", None)
            episode_overview = getattr(episode_details, "overview", None)
            still_path = getattr(episode_details, "still_path", None)
            episode_runtime = _as_int(getattr(episode_details, "runtime", None), 0)
            if episode_title:
                result["episode_title"] = str(episode_title)
            if episode_overview:
                result["episode_overview"] = str(episode_overview)
            if still_path:
                # The per-episode TMDb still takes precedence over any legacy
                # thumbnail stored by a different metadata provider.
                result["episode_backdrop"] = format_tmdb_image(still_path, "original")
            if episode_runtime > 0:
                result["runtime"] = f"{episode_runtime} min (approx.)"
            else:
                fallback_runtimes = sorted({
                    _as_int(value, 0)
                    for value in (getattr(details, "episode_run_time", None) or [])
                    if _as_int(value, 0) > 0
                }) if details else []
                if fallback_runtimes:
                    result["runtime"] = f"{fallback_runtimes[0]} min (approx.)"
                elif result.get("runtime"):
                    result["runtime"] = f"{result['runtime']} (approx.)"
        result["aka"] = (
            f"{result.get('title') or 'Unknown'} "
            f"S{_as_int(result.get('season_number'), 0):02d}E{_as_int(result.get('episode_number'), 0):02d}"
            + (f" - {result.get('episode_title')}" if result.get("episode_title") else "")
        )
    else:
        current_title = str(result.get("title") or "").strip().casefold()
        original_title = str(result.get("original_title") or "").strip().casefold()
        alias = next((
            str(value).strip() for value in (aliases or [])
            if str(value or "").strip().casefold() not in {current_title, original_title}
        ), "")
        if alias:
            result["aka"] = alias
        else:
            result.setdefault("aka", "N/A")

    return result


def _collect_tracked_message_refs(record: dict | None, fallback_chat=None) -> list[dict]:
    """Collect every active/legacy/pending Telegram post as chat/message pairs."""
    record = record or {}
    default_chat = record.get("chat_id") or fallback_chat
    refs: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def add(chat_id, message_id):
        parsed_id = _as_int(message_id)
        if chat_id is None or parsed_id is None or parsed_id <= 0:
            return
        marker = (str(chat_id), parsed_id)
        if marker in seen:
            return
        seen.add(marker)
        refs.append({"chat_id": chat_id, "message_id": parsed_id})

    # The canonical post and V3 alert copies all lived in record.chat_id.
    add(default_chat, record.get("message_id"))
    for message_id in record.get("quality_alert_message_ids") or []:
        add(default_chat, message_id)
    last_alert = record.get("last_quality_alert") or {}
    if isinstance(last_alert, dict):
        add(last_alert.get("chat_id") or default_chat, last_alert.get("message_id"))

    # V4 persists failed deletions individually so they can be retried even if
    # the active announcement moves to another channel later.
    for item in record.get("pending_delete_messages") or []:
        if isinstance(item, dict):
            add(item.get("chat_id") or default_chat, item.get("message_id"))
        else:  # tolerate an early V4/dev record that stored only message ids
            add(default_chat, item)
    return refs


async def _flush_pending_announcement_deletes(key: str) -> bool:
    """Retry queued deletions; retain every failed reference for a later retry."""
    collection = db.dbs["tracking"]["announced"]
    record = await collection.find_one({"_id": key})
    if not record:
        return True

    pending = []
    seen = set()
    for item in record.get("pending_delete_messages") or []:
        if not isinstance(item, dict):
            item = {"chat_id": record.get("chat_id"), "message_id": item}
        chat_id = item.get("chat_id") or record.get("chat_id")
        message_id = _as_int(item.get("message_id"))
        if chat_id is None or message_id is None or message_id <= 0:
            continue
        marker = (str(chat_id), message_id)
        if marker not in seen:
            seen.add(marker)
            pending.append({"chat_id": chat_id, "message_id": message_id})

    if not pending:
        return True

    remaining = []
    for item in pending:
        if not await _delete_telegram_message(item["chat_id"], item["message_id"]):
            remaining.append(item)

    await collection.update_one(
        {"_id": key},
        {"$set": {
            "pending_delete_messages": remaining,
            "updated_at": datetime.utcnow(),
        }},
        upsert=True,
    )
    if remaining:
        LOGGER.warning(
            f"Announcement {key} has {len(remaining)} old Telegram post(s) queued for deletion retry."
        )
    return not remaining


async def _persist_replacement_and_cleanup(
    key: str,
    collection,
    common_fields: dict,
    record: dict | None,
    old_chat,
    new_chat,
    new_message_id: int,
    now: datetime,
    current_qualities: list[str],
    newly_added: list[str],
) -> bool:
    """Persist the newly published post before deleting any previous posts."""
    # Include the previous canonical post, V3 quality-alert copies, and any
    # deletion already pending from an earlier attempt. Preserve chat ids.
    refs = _collect_tracked_message_refs(record, fallback_chat=old_chat)
    refs = [
        item for item in refs
        if not (str(item["chat_id"]) == str(new_chat) and item["message_id"] == int(new_message_id))
    ]

    fields = dict(common_fields)
    fields.update({
        "chat_id": new_chat,
        "message_id": int(new_message_id),
        "published_qualities": current_qualities,
        "created_at": (record or {}).get("created_at") or now,
        "quality_alert_message_ids": [],
        "last_quality_alert": None,
        "pending_delete_messages": refs,
        "last_quality_update": ({
            "qualities": newly_added,
            "sent_at": now,
        } if newly_added else (record or {}).get("last_quality_update")),
    })

    # Do not delete old posts unless MongoDB has saved the new active message id.
    try:
        await collection.update_one({"_id": key}, {"$set": fields}, upsert=True)
    except Exception:
        LOGGER.critical(
            f"New announcement {new_message_id} for {key} was published, but its tracking write failed. "
            "Previous posts were deliberately kept; reconcile MongoDB before retrying.",
            exc_info=True,
        )
        return False

    # New post is now the active tracked announcement. Old posts can be removed.
    await _flush_pending_announcement_deletes(key)
    return True


async def _publish_or_update(key: str, chat, info: dict, season_metadata: dict | None = None) -> bool:
    """Keep one active announcement and replace it when a new quality appears.

    V4 quality-update flow:
      1. Publish a fresh post containing the complete quality list and a
         "NEW QUALITY ADDED" banner.
      2. Persist its message id as the active announcement in MongoDB.
      3. Delete the old canonical post and all V3 alert copies.

    The old posts are never deleted before both the new Telegram post and its
    MongoDB tracking record are confirmed. Failed deletions remain queued.
    """
    lock = _ANNOUNCEMENT_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        info = await _enrich_announcement_info(info)
        current_qualities = _normalise_quality_names(
            info.get("qualities") or ([info.get("quality")] if info.get("quality") else [])
        )
        if current_qualities:
            info["qualities"] = current_qualities

        collection = db.dbs["tracking"]["announced"]
        record = await collection.find_one({"_id": key})
        poster_urls = await _get_posters(info, season_metadata)
        message_id = (record or {}).get("message_id")
        old_chat = (record or {}).get("chat_id") or chat

        # V2 migration uses the database's pre-ingestion snapshot. V3+ records
        # persist the exact quality set shown in their active announcement.
        stored_snapshot = (record or {}).get("published_qualities")
        if isinstance(stored_snapshot, list):
            previous_qualities = _normalise_quality_names(stored_snapshot)
        elif "_previous_qualities" in info and info.get("_previous_qualities") is not None:
            previous_qualities = _normalise_quality_names(info.get("_previous_qualities"))
        else:
            previous_qualities = None

        newly_added = _new_quality_labels(current_qualities, previous_qualities or [])
        has_baseline = previous_qualities is not None
        # Do not call the first-ever announcement a quality update. Existing V3
        # tracking rows count as published even if their post was deleted manually.
        is_quality_update = bool(record and newly_added and has_baseline)
        now = datetime.utcnow()

        common_fields = {
            "updated_at": now,
            "kind": info.get("announcement_kind"),
            "tmdb_id": info.get("tmdb_id"),
            "media_type": info.get("media_type"),
            "season_number": info.get("season_number"),
            "episode_number": info.get("episode_number"),
        }
        if season_metadata:
            common_fields.update({
                "episode_numbers": sorted(season_metadata.get("episode_numbers") or []),
                "season_poster_path": season_metadata.get("poster_path") or "",
                "season_overview": season_metadata.get("season_overview") or "",
                "season_name": season_metadata.get("season_name") or "",
            })

        # A genuine quality addition is a replacement transaction, not an edit
        # followed by another copy. The original remains until the fresh post
        # and its new MongoDB message id are safely stored.
        if is_quality_update:
            replacement_info = dict(info)
            replacement_info["newly_added_qualities"] = newly_added
            new_message_id = await _send_announcement(chat, replacement_info, poster_urls)
            if not new_message_id:
                LOGGER.warning(
                    f"Quality replacement post failed for {key}; keeping the old announcement intact."
                )
                return False
            return await _persist_replacement_and_cleanup(
                key, collection, common_fields, record, old_chat, chat,
                new_message_id, now, current_qualities, newly_added,
            )

        # No new quality: update the current active post in place. This branch
        # also cleans up duplicate V3 alert copies left by the previous version.
        if message_id:
            if await _edit_announcement(old_chat, message_id, info, poster_urls):
                active_ref = (str(old_chat), int(message_id))
                stale_refs = [
                    item for item in _collect_tracked_message_refs(record, fallback_chat=old_chat)
                    if (str(item["chat_id"]), item["message_id"]) != active_ref
                ]
                pending = stale_refs
                # Persist the active canonical post and cleanup queue first.
                await collection.update_one(
                    {"_id": key},
                    {"$set": {
                        **common_fields,
                        "chat_id": old_chat,
                        "message_id": int(message_id),
                        "published_qualities": current_qualities,
                        "quality_alert_message_ids": [],
                        "last_quality_alert": None,
                        "pending_delete_messages": pending,
                    }},
                    upsert=True,
                )
                await _flush_pending_announcement_deletes(key)
                return True

        # Initial post, or recovery when Telegram says the active post cannot
        # be edited. If this is still a quality update, preserve the banner.
        replacement_info = dict(info)
        if is_quality_update:
            replacement_info["newly_added_qualities"] = newly_added
        new_message_id = await _send_announcement(chat, replacement_info, poster_urls)
        if not new_message_id:
            return False
        return await _persist_replacement_and_cleanup(
            key, collection, common_fields, record, old_chat, chat,
            new_message_id, now, current_qualities,
            newly_added if is_quality_update else [],
        )


async def _get_tmdb_season_progress(tmdb_id, season_number: int) -> dict | None:
    """Return complete TMDb episode metadata split into aired and not-yet-aired.

    A complete metadata response is required before issuing missing-episode
    alerts. Episodes without a valid air date are deliberately not classified
    as aired, preventing false alerts for upcoming or not-yet-scheduled episodes.
    """
    tmdb_id_int = _as_int(tmdb_id)
    if not tmdb_id_int or season_number <= 0:
        return None

    cache_key = (tmdb_id_int, season_number)
    cached = _SEASON_PROGRESS_CACHE.get(cache_key)
    if cached:
        cache_age = time.monotonic() - cached[0]
        cache_ttl = _SEASON_NEGATIVE_CACHE_TTL if cached[1] is None else _SEASON_CACHE_TTL
        if cache_age < cache_ttl:
            return cached[1]

    api_key = tmdb_provider.tmdb_api_key()
    if not api_key:
        LOGGER.warning("Season episode checks are waiting: TMDb API key is not configured.")
        _SEASON_PROGRESS_CACHE[cache_key] = (time.monotonic(), None)
        return None

    base = "https://api.themoviedb.org/3"
    try:
        async with tmdb_provider.API_SEMAPHORE:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                show_response, season_response = await asyncio.gather(
                    client.get(
                        f"{base}/tv/{tmdb_id_int}",
                        params={"api_key": api_key, "language": "en-US"},
                    ),
                    client.get(
                        f"{base}/tv/{tmdb_id_int}/season/{season_number}",
                        params={"api_key": api_key, "language": "en-US"},
                    ),
                )

        if show_response.status_code != 200 or season_response.status_code != 200:
            LOGGER.debug(
                f"TMDb season metadata unavailable for tv={tmdb_id_int} season={season_number}: "
                f"show={show_response.status_code}, season={season_response.status_code}"
            )
            _SEASON_PROGRESS_CACHE[cache_key] = (time.monotonic(), None)
            return None

        show_data = show_response.json()
        season_data = season_response.json()
        season_summary = next((
            item for item in (show_data.get("seasons") or [])
            if _as_int(item.get("season_number"), -1) == season_number
        ), None)
        expected_count = _as_int((season_summary or {}).get("episode_count"), 0)
        metadata_episodes = season_data.get("episodes") or []
        if expected_count <= 0:
            expected_count = len(metadata_episodes)

        # Never alert from a partial TMDb response: it could omit an aired EP.
        if not metadata_episodes or (expected_count > 0 and len(metadata_episodes) < expected_count):
            LOGGER.debug(
                f"TMDb season metadata incomplete for tv={tmdb_id_int} S{season_number:02d}: "
                f"returned {len(metadata_episodes)}/{expected_count} episode records"
            )
            _SEASON_PROGRESS_CACHE[cache_key] = (time.monotonic(), None)
            return None

        # The application logs use IST; compare date-only TMDb air dates against
        # the same local calendar date so an episode airing today is not future.
        try:
            today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
        except Exception:
            today = date.today()
        aired_episodes: dict[int, dict] = {}
        has_unreleased_or_unknown_regular_episode = False
        for episode in metadata_episodes:
            number = _as_int(episode.get("episode_number"), 0)
            episode_type = str(episode.get("episode_type") or "standard").lower()
            if number <= 0 or number >= 1000 or episode_type == "special":
                continue

            air_date = str(episode.get("air_date") or "").strip()
            if not air_date:
                has_unreleased_or_unknown_regular_episode = True
                continue
            try:
                parsed_date = date.fromisoformat(air_date[:10])
            except ValueError:
                LOGGER.debug(
                    f"TMDb season metadata has invalid air_date={air_date!r} "
                    f"for tv={tmdb_id_int} S{season_number:02d}E{number:02d}"
                )
                has_unreleased_or_unknown_regular_episode = True
                continue

            # TMDb supplies a date but not a reliable per-episode release time.
            # Treat today's date as not-yet-confirmed so an upcoming episode
            # scheduled later today is never included in a missing alert.
            if parsed_date >= today:
                has_unreleased_or_unknown_regular_episode = True
                continue
            aired_episodes[number] = episode

        result = {
            "aired_episodes": aired_episodes,
            "episode_numbers": set(aired_episodes),
            "all_regular_episodes_aired": not has_unreleased_or_unknown_regular_episode,
            "season_name": str(season_data.get("name") or f"Season {season_number}"),
            "series_name": str(show_data.get("name") or show_data.get("original_name") or ""),
            "year": str(show_data.get("first_air_date") or "")[:4],
            "season_overview": str(season_data.get("overview") or ""),
            "poster_path": str(season_data.get("poster_path") or ""),
            "expected_count": expected_count,
        }
        _SEASON_PROGRESS_CACHE[cache_key] = (time.monotonic(), result)
        return result
    except Exception as e:
        LOGGER.warning(
            f"TMDb season metadata check failed for tv={tmdb_id_int} "
            f"season={season_number} ({type(e).__name__})."
        )
        _SEASON_PROGRESS_CACHE[cache_key] = (time.monotonic(), None)
        return None


async def _get_tmdb_season_metadata(tmdb_id, season_number: int) -> dict | None:
    """Return a season checklist only after every regular episode has aired.

    Consolidation remains conservative. The separate progress helper can still
    identify already-aired missing episodes while a season has future dates.
    """
    tmdb_id_int = _as_int(tmdb_id)
    if not tmdb_id_int or season_number <= 0:
        return None

    cache_key = (tmdb_id_int, season_number)
    cached = _SEASON_METADATA_CACHE.get(cache_key)
    if cached:
        cache_age = time.monotonic() - cached[0]
        cache_ttl = _SEASON_NEGATIVE_CACHE_TTL if cached[1] is None else _SEASON_CACHE_TTL
        if cache_age < cache_ttl:
            return cached[1]

    progress = await _get_tmdb_season_progress(tmdb_id_int, season_number)
    if not progress or not progress.get("all_regular_episodes_aired"):
        _SEASON_METADATA_CACHE[cache_key] = (time.monotonic(), None)
        return None

    episode_numbers = progress.get("episode_numbers") or set()
    if not episode_numbers:
        _SEASON_METADATA_CACHE[cache_key] = (time.monotonic(), None)
        return None

    result = {
        "episode_numbers": set(episode_numbers),
        "season_name": progress.get("season_name") or f"Season {season_number}",
        "season_overview": progress.get("season_overview") or "",
        "poster_path": progress.get("poster_path") or "",
        "expected_count": progress.get("expected_count") or len(episode_numbers),
    }
    _SEASON_METADATA_CACHE[cache_key] = (time.monotonic(), result)
    return result


def _missing_episode_approver_ids() -> list[int]:
    """Resolve recipients strictly from the configured subscription approvers."""
    try:
        recipients = list(SettingsManager.current().approver_ids or Telegram.APPROVER_IDS or [])
    except Exception:
        recipients = list(Telegram.APPROVER_IDS or [])

    result = []
    seen = set()
    for value in recipients:
        user_id = _as_int(value, 0)
        if user_id > 0 and user_id not in seen:
            seen.add(user_id)
            result.append(user_id)
    return result


def _quality_tiers_present(qualities) -> set[str]:
    """Map raw stream labels to independent audit tiers.

    480p is its own tier. 540p is accepted as a substitute only for the 720p
    tier. 1080p and any higher recognised resolution satisfy the 1080p tier.
    Higher tiers never satisfy lower tiers.
    """
    present: set[str] = set()
    for raw in qualities or []:
        label = str(raw or "").strip().lower()
        if not label:
            continue
        if re.search(r"(?<!\d)8\s*k(?!\w)", label):
            present.add("1080p")
            continue
        if re.search(r"(?<!\d)4\s*k(?!\w)", label):
            present.add("1080p")
            continue
        match = re.search(r"(?<!\d)(\d{3,4})\s*p?\b", label)
        if not match:
            continue
        resolution = int(match.group(1))
        if resolution == 480:
            present.add("480p")
        elif resolution in (540, 720):
            present.add("720p")
        elif resolution >= 1080:
            present.add("1080p")
    return present


def _build_season_quality_audit(season: dict, aired_episode_numbers: set[int]) -> dict:
    """Return missing episodes and independent quality gaps for aired episodes."""
    expected = {_as_int(number, 0) for number in aired_episode_numbers if _as_int(number, 0) > 0}
    episode_by_number = {
        _as_int(item.get("episode_number"), 0): item
        for item in (season or {}).get("episodes") or []
        if _as_int(item.get("episode_number"), 0) > 0
    }
    completely_missing: set[int] = set()
    quality_gaps: dict[int, dict] = {}
    indexed_count = 0

    for number in sorted(expected):
        episode = episode_by_number.get(number) or {}
        live_streams = [
            stream for stream in (episode.get("telegram") or [])
            if isinstance(stream, dict) and not stream.get("is_dead")
        ]
        if not live_streams:
            completely_missing.add(number)
            continue

        indexed_count += 1
        available = sorted({
            str(stream.get("quality") or "").strip()
            for stream in live_streams
            if str(stream.get("quality") or "").strip()
        }, key=_quality_sort_key)
        present_tiers = _quality_tiers_present(available)
        missing_tiers = [
            tier for tier in ("480p", "720p", "1080p")
            if tier not in present_tiers
        ]
        if missing_tiers:
            quality_gaps[number] = {
                "available_qualities": available,
                "missing_tiers": missing_tiers,
            }

    return {
        "missing_episode_numbers": completely_missing,
        "indexed_count": indexed_count,
        "quality_gaps": quality_gaps,
    }


def _build_missing_episode_telegraph_content(
    info: dict,
    season_number: int,
    progress: dict,
    missing_numbers: list[int],
    quality_gaps: dict[int, dict],
    indexed_count: int,
) -> list[dict]:
    """Build a Telegraph-compatible rich document for one season audit.

    Telegraph's supported NodeElement tags do not include HTML tables. Use
    consistent episode sections with bold labels instead of emitting an
    unsupported <table> element that Telegraph would reject or strip.
    """
    tmdb_id = _as_int(info.get("tmdb_id"), 0)
    title = str(info.get("title") or progress.get("series_name") or "Unknown TV Show").strip()
    year = _as_int(info.get("year"), _as_int(progress.get("year"), 0))
    title_year = f"{title} ({year})" if year else title
    aired_episodes = progress.get("aired_episodes") or {}
    missing_set = {_as_int(number, 0) for number in missing_numbers}
    gap_numbers = {_as_int(number, 0) for number in quality_gaps}
    issue_numbers = sorted(number for number in missing_set | gap_numbers if number > 0)
    aired_count = len(progress.get("episode_numbers") or [])
    total_quality_gaps = sum(len(gap.get("missing_tiers") or []) for gap in quality_gaps.values())

    nodes: list[dict] = [
        {"tag": "h3", "children": ["⚠️ Missing Episodes & Quality Audit"]},
        {"tag": "p", "children": [
            {"tag": "strong", "children": [f"📺 {title_year}"]},
            " · ",
            {"tag": "a", "attrs": {"href": f"https://www.themoviedb.org/tv/{tmdb_id}"},
             "children": [f"TMDb ID: {tmdb_id}"]},
        ]},
        {"tag": "h4", "children": [f"Season {season_number:02d} — Episode & Quality Audit"]},
        {"tag": "p", "children": [
            {"tag": "strong", "children": ["📦 Season coverage: "]},
            f"{indexed_count}/{aired_count} aired episodes indexed · ",
            f"{len(missing_set)} completely missing · ",
            f"{len(quality_gaps)} with quality gaps · {total_quality_gaps} missing quality tiers",
        ]},
        {"tag": "hr"},
    ]

    if not issue_numbers:
        nodes.extend([
            {"tag": "h4", "children": ["✅ All audited episodes are complete"]},
            {"tag": "p", "children": [
                "Every aired regular episode is indexed and has all three required quality tiers."
            ]},
        ])
    else:
        for number in issue_numbers:
            episode = aired_episodes.get(number) or {}
            episode_title = str(episode.get("name") or "Untitled episode").strip()
            code = f"S{season_number:02d}E{number:02d}"
            if number in missing_set:
                available_text = "No active streams"
                missing_text = "ALL MISSING"
                marker = "🔴"
            else:
                gap = quality_gaps[number]
                available_text = " | ".join(gap.get("available_qualities") or []) or "Unclassified"
                missing_text = " | ".join(gap.get("missing_tiers") or [])
                marker = "🟠"
            nodes.extend([
                {"tag": "h4", "children": [f"{marker} {code} — {episode_title}"]},
                {"tag": "p", "children": [
                    {"tag": "strong", "children": ["Available: "]},
                    available_text,
                    {"tag": "br"},
                    {"tag": "strong", "children": ["Missing: "]},
                    missing_text,
                ]},
                {"tag": "hr"},
            ])

    nodes.extend([
        {"tag": "h4", "children": ["Quality audit rules"]},
        {"tag": "ul", "children": [
            {"tag": "li", "children": ["480p is an independent required tier."]},
            {"tag": "li", "children": ["720p is satisfied by 720p or 540p."]},
            {"tag": "li", "children": ["1080p is satisfied by 1080p, 1440p, 2160p, 4K, 8K or higher."]},
            {"tag": "li", "children": ["Higher tiers do not replace the lower required tiers."]},
            {"tag": "li", "children": ["Dead streams are excluded from the audit."]},
        ]},
        {"tag": "blockquote", "children": [
            "Only regular episodes with an air date before today's IST date are included. "
            "Episodes dated today or in the future, specials, and episodes without a valid air date are excluded."
        ]},
    ])
    return nodes


class _TelegraphAPIError(RuntimeError):
    def __init__(self, method: str, error: str):
        self.method = method
        self.error = str(error or "Unknown Telegraph API error")
        super().__init__(f"Telegraph API {method} failed: {self.error}")


async def _telegraph_api_request(method: str, payload: dict) -> dict:
    """Call the official Telegraph API using its form-encoded POST interface."""
    url = f"{_TELEGRAPH_API_BASE}/{method}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = await client.post(url, data=payload)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict) or not body.get("ok"):
        raise _TelegraphAPIError(method, (body or {}).get("error") or response.text)
    result = body.get("result")
    if not isinstance(result, dict):
        raise _TelegraphAPIError(method, "API returned an invalid result")
    return result


async def _get_telegraph_access_token() -> str:
    """Get or lazily provision the app's Telegraph account token in MongoDB."""
    collection = db.dbs["tracking"]["telegraph_accounts"]
    record_id = "season_quality_audit"
    record = await collection.find_one({"_id": record_id})
    token = str((record or {}).get("access_token") or "").strip()
    if token:
        return token

    async with _TELEGRAPH_ACCOUNT_LOCK:
        record = await collection.find_one({"_id": record_id})
        token = str((record or {}).get("access_token") or "").strip()
        if token:
            return token

        account = await _telegraph_api_request("createAccount", {
            "short_name": "TGStremioAudit",
            "author_name": "Telegram-Stremio",
        })
        token = str(account.get("access_token") or "").strip()
        if not token:
            raise _TelegraphAPIError("createAccount", "No access_token was returned")
        await collection.update_one(
            {"_id": record_id},
            {"$set": {
                "access_token": token,
                "short_name": "TGStremioAudit",
                "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
            }},
            upsert=True,
        )
        return token


async def _create_or_update_telegraph_audit_page(
    tmdb_id: int,
    season_number: int,
    title: str,
    content: list[dict],
    has_issues: bool | None = None,
) -> dict:
    """Publish or update one stable Telegraph page per TV show/season."""
    token = await _get_telegraph_access_token()
    pages = db.dbs["tracking"]["season_missing_episode_telegraph_pages"]
    page_id = f"tv:{tmdb_id}:season:{season_number:02d}"
    old_page = await pages.find_one({"_id": page_id})
    page_title = f"{title} — Season {season_number:02d} Episode & Quality Audit"[:256]
    content_json = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    if len(content_json.encode("utf-8")) > 64 * 1024:
        raise _TelegraphAPIError("createPage/editPage", "Audit page content exceeds Telegraph's 64 KB limit")

    page = None
    old_path = str((old_page or {}).get("path") or "").strip()
    if old_path:
        try:
            page = await _telegraph_api_request(
                f"editPage/{quote(old_path, safe='')}",
                {
                    "access_token": token,
                    "title": page_title,
                    "content": content_json,
                    "author_name": "Telegram-Stremio",
                    "return_content": "false",
                },
            )
        except _TelegraphAPIError as error:
            if error.error not in {"PAGE_NOT_FOUND", "PAGE_NOT_EDITABLE"}:
                raise
            LOGGER.warning(
                f"Stored Telegraph audit page is no longer editable for {page_id}; creating a new page."
            )

    if page is None:
        page = await _telegraph_api_request("createPage", {
            "access_token": token,
            "title": page_title,
            "content": content_json,
            "author_name": "Telegram-Stremio",
            "return_content": "false",
        })

    page_url = str(page.get("url") or "").strip()
    page_path = str(page.get("path") or "").strip()
    if not page_url or not page_path:
        raise _TelegraphAPIError("createPage/editPage", "Page URL or path missing from response")

    await pages.update_one(
        {"_id": page_id},
        {"$set": {
            "tmdb_id": tmdb_id,
            "season_number": season_number,
            "title": page_title,
            "url": page_url,
            "path": page_path,
            "has_open_issues": has_issues if has_issues is not None else bool((old_page or {}).get("has_open_issues", True)),
            "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }},
        upsert=True,
    )
    return {"url": page_url, "path": page_path, "title": page_title}


async def _notify_approvers_missing_episodes(
    info: dict,
    season_number: int,
    progress: dict,
    missing_numbers: set[int],
    indexed_count: int,
    quality_gaps: dict[int, dict] | None = None,
) -> None:
    """Send compact Telegram notices linking to a rich Telegraph audit page.

    The Telegraph page is shared and updated in place for each show/season.
    Per-approver deduplication continues to track the episode and quality state.
    """
    tmdb_id = _as_int(info.get("tmdb_id"), 0)
    if not tmdb_id:
        return

    recipients = _missing_episode_approver_ids()
    if not recipients:
        LOGGER.debug(
            f"Missing-episode alert skipped for tv={tmdb_id} S{season_number:02d}: "
            "APPROVER_IDS is empty."
        )
        return

    sorted_missing = sorted(
        _as_int(number, 0) for number in missing_numbers if _as_int(number, 0) > 0
    )
    quality_gaps = quality_gaps or {}
    quality_state = [
        {
            "episode_number": int(number),
            "available_qualities": list(gap.get("available_qualities") or []),
            "missing_tiers": list(gap.get("missing_tiers") or []),
        }
        for number, gap in sorted(quality_gaps.items(), key=lambda item: int(item[0]))
    ]
    missing_state = sorted_missing
    has_issues = bool(missing_state or quality_state)
    record_key = f"tv:{tmdb_id}:season:{season_number:02d}"
    alert_collection = db.dbs["tracking"]["season_missing_episode_alerts"]
    title = str(info.get("title") or progress.get("series_name") or "Unknown TV Show").strip()
    year = _as_int(info.get("year"), _as_int(progress.get("year"), 0))
    title_year = f"{title} ({year})" if year else title
    alert_format_version = 3
    changed_recipients: list[tuple[int, str]] = []

    for user_id in recipients:
        alert_id = f"{record_key}:approver:{user_id}"
        lock = _ANNOUNCEMENT_LOCKS.setdefault(f"missing-alert:{alert_id}", asyncio.Lock())
        async with lock:
            try:
                previous = await alert_collection.find_one({"_id": alert_id})
                previous_missing = sorted(
                    _as_int(value, 0)
                    for value in (previous or {}).get("missing_episode_numbers", [])
                )
                previous_quality_state = (previous or {}).get("missing_quality_tiers") or []
                same_state = (
                    previous is not None
                    and previous.get("alert_format_version") == alert_format_version
                    and previous_missing == missing_state
                    and previous_quality_state == quality_state
                )

                if has_issues:
                    if not (same_state and previous.get("delivery_status") == "sent"):
                        changed_recipients.append((user_id, alert_id))
                else:
                    if not same_state or previous.get("delivery_status") != "resolved":
                        await alert_collection.update_one(
                            {"_id": alert_id},
                            {"$set": {
                                "tmdb_id": tmdb_id,
                                "title": title,
                                "year": year or None,
                                "season_number": season_number,
                                "approver_id": user_id,
                                "missing_episode_numbers": [],
                                "missing_quality_tiers": [],
                                "alert_format_version": alert_format_version,
                                "delivery_status": "resolved",
                                "resolved_at": datetime.now(timezone.utc).replace(tzinfo=None),
                                "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
                            }},
                            upsert=True,
                        )
            except Exception as e:
                LOGGER.warning(
                    f"Could not inspect missing episode audit state for approver {user_id}, "
                    f"tv={tmdb_id} S{season_number:02d}: {e}"
                )

    page_key = f"tv:{tmdb_id}:season:{season_number:02d}"
    page_collection = db.dbs["tracking"]["season_missing_episode_telegraph_pages"]
    page_record = await page_collection.find_one({"_id": page_key})

    if not has_issues:
        # Keep an already-shared Telegraph URL truthful after the audit is fixed.
        # The page flag makes failed resolution edits retry on the next reconciliation.
        if page_record and page_record.get("has_open_issues", True):
            try:
                cleared_content = _build_missing_episode_telegraph_content(
                    info, season_number, progress, [], {}, indexed_count
                )
                await _create_or_update_telegraph_audit_page(
                    tmdb_id, season_number, title_year, cleared_content, has_issues=False
                )
            except Exception as e:
                LOGGER.warning(
                    f"Could not mark Telegraph audit resolved for tv={tmdb_id} "
                    f"S{season_number:02d}; it will retry: {e}"
                )
        return

    if not changed_recipients:
        return

    try:
        content = _build_missing_episode_telegraph_content(
            info,
            season_number,
            progress,
            sorted_missing,
            quality_gaps,
            indexed_count,
        )
        page = await _create_or_update_telegraph_audit_page(
            tmdb_id, season_number, title_year, content, has_issues=True
        )
    except Exception as e:
        LOGGER.warning(
            f"Failed to publish Telegraph episode/quality audit for tv={tmdb_id} "
            f"S{season_number:02d}: {e}"
        )
        return

    aired_count = len(progress.get("episode_numbers") or [])
    total_quality_gaps = sum(len(gap.get("missing_tiers") or []) for gap in quality_gaps.values())
    summary = (
        "⚠️ <b>Media Library Audit Updated</b>\n"
        f"📺 <b>{html.escape(title_year)}</b>\n"
        f"<b>Season {season_number:02d}</b> · {indexed_count}/{aired_count} aired episodes indexed\n"
        f"🔴 {len(sorted_missing)} completely missing · 🟠 "
        f"{len(quality_state)} episodes with quality gaps · {total_quality_gaps} missing tiers"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 Open full audit", url=page["url"])
    ]])

    for user_id, alert_id in changed_recipients:
        try:
            await StreamBot.send_message(
                chat_id=user_id,
                text=summary,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            await alert_collection.update_one(
                {"_id": alert_id},
                {"$set": {
                    "tmdb_id": tmdb_id,
                    "title": title,
                    "year": year or None,
                    "season_number": season_number,
                    "approver_id": user_id,
                    "missing_episode_numbers": missing_state,
                    "missing_quality_tiers": quality_state,
                    "indexed_aired_episode_count": indexed_count,
                    "expected_aired_episode_count": aired_count,
                    "alert_format_version": alert_format_version,
                    "delivery_status": "sent",
                    "telegraph_url": page["url"],
                    "telegraph_path": page["path"],
                    "notified_at": datetime.now(timezone.utc).replace(tzinfo=None),
                    "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
                }},
                upsert=True,
            )
            LOGGER.info(
                f"Sent Telegraph missing episode/quality audit link to approver {user_id} for "
                f"tv={tmdb_id} S{season_number:02d}: missing episodes={missing_state}, "
                f"quality-gap episodes={[item['episode_number'] for item in quality_state]}"
            )
        except Exception as e:
            LOGGER.warning(
                f"Failed to send Telegraph audit link to approver {user_id} for "
                f"tv={tmdb_id} S{season_number:02d}: {e}"
            )


def _build_episode_info(info: dict, doc: dict | None, season_number: int, episode_number: int) -> dict:
    result = _base_info_from_doc(doc, "tv") if doc else dict(info)
    result.update({
        "announcement_kind": "episode",
        "season_number": season_number,
        "episode_number": episode_number,
        "_previous_qualities": info.get("_previous_qualities"),
    })
    episode = _find_episode(doc, season_number, episode_number) if doc else None
    result["episode_title"] = (episode or {}).get("title") or info.get("episode_title") or ""
    result["episode_overview"] = (episode or {}).get("overview") or info.get("episode_overview") or ""
    result["episode_backdrop"] = (episode or {}).get("episode_backdrop") or info.get("episode_backdrop") or ""
    result["qualities"] = _episode_qualities(episode) or ([info.get("quality")] if info.get("quality") else [])
    return result


def _build_season_info(
    info: dict,
    doc: dict | None,
    season_number: int,
    episode_numbers: set[int] | None = None,
    season_metadata: dict | None = None,
) -> dict:
    result = _base_info_from_doc(doc, "tv") if doc else dict(info)
    season = _find_season(doc, season_number) if doc else None
    result.update({
        "announcement_kind": "season",
        "season_number": season_number,
        "_previous_qualities": info.get("_previous_season_qualities"),
        "season_name": (season_metadata or {}).get("season_name") or f"Season {season_number}",
        "season_overview": (season_metadata or {}).get("season_overview") or "",
        "season_poster_path": (season_metadata or {}).get("poster_path") or "",
        "qualities": _season_qualities(season, episode_numbers),
    })
    return result


async def _delete_tracked_announcement_messages(record: dict) -> bool:
    """Delete the active post plus every V3/V4 superseded message reference."""
    refs = _collect_tracked_message_refs(record)
    deleted_all = True
    for item in refs:
        if not await _delete_telegram_message(item["chat_id"], item["message_id"]):
            deleted_all = False
    return deleted_all


async def _retry_pending_announcement_deletes() -> None:
    """Retry old-post deletions that failed after a V4 replacement."""
    collection = db.dbs["tracking"]["announced"]
    cursor = collection.find({
        "pending_delete_messages": {"$exists": True, "$ne": []},
    })
    async for record in cursor:
        key = str(record.get("_id") or "")
        if not key:
            continue
        lock = _ANNOUNCEMENT_LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                await _flush_pending_announcement_deletes(key)
            except Exception as e:
                LOGGER.warning(f"Pending announcement cleanup failed for {key}: {e}")


async def _remove_legacy_tv_announcement(tmdb_id) -> None:
    """Remove the old one-post-per-series tracking entry during migration."""
    key = f"tv:{tmdb_id}"
    collection = db.dbs["tracking"]["announced"]
    record = await collection.find_one({"_id": key})
    if not record:
        return
    if await _delete_tracked_announcement_messages(record):
        await collection.delete_one({"_id": key})


async def _cleanup_episode_announcements(tmdb_id, season_number: int, season_key: str) -> None:
    """Remove episode posts only after the consolidated season post exists."""
    collection = db.dbs["tracking"]["announced"]
    prefix = re.escape(f"tv:{tmdb_id}:episode:{season_number:02d}:")
    cursor = collection.find({"_id": {"$regex": f"^{prefix}"}})
    async for record in cursor:
        if await _delete_tracked_announcement_messages(record):
            await collection.update_one(
                {"_id": record["_id"]},
                {"$set": {
                    "message_id": None,
                    "quality_alert_message_ids": [],
                    "last_quality_alert": None,
                    "pending_delete_messages": [],
                    "superseded_by": season_key,
                    "updated_at": datetime.utcnow(),
                }},
            )
        else:
            await collection.update_one(
                {"_id": record["_id"]},
                {"$set": {"superseded_by": season_key, "updated_at": datetime.utcnow()}},
            )


async def _retry_superseded_episode_deletes() -> None:
    """Retry Telegram deletes that failed during an earlier season consolidation."""
    collection = db.dbs["tracking"]["announced"]
    cursor = collection.find({
        "_id": {"$regex": r"^tv:\d+:episode:"},
        "superseded_by": {"$exists": True},
        "message_id": {"$ne": None},
    })
    async for record in cursor:
        if await _delete_tracked_announcement_messages(record):
            await collection.update_one(
                {"_id": record["_id"]},
                {"$set": {
                    "message_id": None,
                    "quality_alert_message_ids": [],
                    "last_quality_alert": None,
                    "pending_delete_messages": [],
                    "updated_at": datetime.utcnow(),
                }},
            )


async def _maybe_consolidate_season(info: dict, doc: dict, chat) -> bool:
    tmdb_id = info.get("tmdb_id")
    season_number = _as_int(info.get("season_number"), 0)
    if not tmdb_id or season_number <= 0:
        return False

    season = _find_season(doc, season_number)
    if not season:
        LOGGER.warning(
            f"Season announcement cannot run for tv={tmdb_id} S{season_number:02d}: "
            "season is absent from the indexed media document."
        )
        return False

    # Progress includes already-aired episodes even when the same season also
    # contains future episodes. That lets us alert approvers without treating
    # the season as complete prematurely.
    progress = await _get_tmdb_season_progress(tmdb_id, season_number)
    if progress:
        aired_numbers = set(progress.get("episode_numbers") or [])
        quality_audit = _build_season_quality_audit(season, aired_numbers)
        missing_aired_numbers = quality_audit["missing_episode_numbers"]
        await _notify_approvers_missing_episodes(
            info,
            season_number,
            progress,
            missing_aired_numbers,
            quality_audit["indexed_count"],
            quality_audit["quality_gaps"],
        )

    # Keep the existing season-completion rule: no consolidated season post
    # until every regular metadata episode has actually aired.
    metadata = await _get_tmdb_season_metadata(tmdb_id, season_number)
    if not metadata:
        LOGGER.debug(
            f"Season announcement pending for tv={tmdb_id} S{season_number:02d}: "
            "TMDb has not returned a complete released-episode checklist yet."
        )
        return False

    expected_numbers = metadata["episode_numbers"]
    indexed_numbers = {
        number for episode in (season.get("episodes") or [])
        if (number := _as_int(episode.get("episode_number"), 0)) in expected_numbers
        and any(not item.get("is_dead") for item in (episode.get("telegram") or []))
    }
    if not expected_numbers.issubset(indexed_numbers):
        missing_numbers = sorted(expected_numbers - indexed_numbers)
        LOGGER.info(
            f"Season announcement pending for tv={tmdb_id} S{season_number:02d}: "
            f"indexed {len(indexed_numbers)}/{len(expected_numbers)} released metadata episodes; "
            f"missing episode numbers={missing_numbers}"
        )
        return False

    key = _season_key(tmdb_id, season_number)
    season_info = _build_season_info(info, doc, season_number, expected_numbers, metadata)
    if not await _publish_or_update(key, chat, season_info, metadata):
        return False

    # Only remove episode posts after the consolidated season post is confirmed.
    await _cleanup_episode_announcements(tmdb_id, season_number, key)
    LOGGER.info(
        f"Published consolidated season announcement for tv={tmdb_id} S{season_number:02d} "
        f"({len(expected_numbers)} metadata episodes)."
    )
    return True


async def _announce_movie(info: dict, chat) -> None:
    tmdb_id = info.get("tmdb_id")
    if not tmdb_id:
        return
    location = await db.find_media_doc("movie", tmdb_id)
    doc = location[0] if location else None
    announcement = _base_info_from_doc(doc, "movie") if doc else dict(info)
    announcement.update({
        "announcement_kind": "movie",
        "qualities": _movie_qualities(doc) or ([info.get("quality")] if info.get("quality") else []),
        "_previous_qualities": info.get("_previous_qualities"),
    })
    await _publish_or_update(f"movie:{tmdb_id}", chat, announcement)


async def _announce_episode(info: dict, chat) -> None:
    tmdb_id = info.get("tmdb_id")
    season_number = _as_int(info.get("season_number"), None)
    episode_number = _as_int(info.get("episode_number"), None)
    if not tmdb_id or season_number is None or episode_number is None:
        return
    # Synthetic combined packs use S00E1000+; don't make them look like episodes.
    if episode_number >= 1000:
        return

    location = await db.find_media_doc("tv", tmdb_id)
    doc = location[0] if location else None
    if not doc:
        LOGGER.warning(f"Cannot build TV announcement: indexed show {tmdb_id} was not found")
        return

    # Once a season has been consolidated, every later quality addition edits
    # that season post rather than recreating an episode post.
    consolidated_key = _season_key(tmdb_id, season_number)
    consolidated = await db.dbs["tracking"]["announced"].find_one({"_id": consolidated_key})
    if consolidated:
        season_metadata = await _get_tmdb_season_metadata(tmdb_id, season_number)
        episode_numbers = (
            set(consolidated.get("episode_numbers") or [])
            or (season_metadata or {}).get("episode_numbers")
        )
        season_info = _build_season_info(
            info, doc, season_number, episode_numbers, season_metadata,
        )
        # Retain the season art and plot even if TMDb is temporarily unavailable.
        season_info["season_poster_path"] = (
            season_info.get("season_poster_path")
            or consolidated.get("season_poster_path")
            or ""
        )
        season_info["season_overview"] = (
            season_info.get("season_overview")
            or consolidated.get("season_overview")
            or ""
        )
        season_info["season_name"] = (
            season_info.get("season_name")
            or consolidated.get("season_name")
            or f"Season {season_number}"
        )
        if await _publish_or_update(consolidated_key, chat, season_info, season_metadata):
            await _cleanup_episode_announcements(tmdb_id, season_number, consolidated_key)
            await _remove_legacy_tv_announcement(tmdb_id)
        return

    # Check completion before posting this episode. If it completes the season,
    # subscribers receive the consolidated season post, not a transient final-EP post.
    if season_number > 0 and await _maybe_consolidate_season(info, doc, chat):
        await _remove_legacy_tv_announcement(tmdb_id)
        return

    key = _episode_key(tmdb_id, season_number, episode_number)
    episode_info = _build_episode_info(info, doc, season_number, episode_number)
    if await _publish_or_update(key, chat, episode_info):
        # Retire the old one-post-per-series message only after its replacement exists.
        await _remove_legacy_tv_announcement(tmdb_id)


async def _reconcile_pending_seasons_once() -> None:
    """Recheck announced, un-consolidated seasons as TMDb air dates change."""
    settings = SettingsManager.current()
    chat = _resolve_chat(settings.announcement_channel)
    if not settings.announce_new_content or chat is None:
        return

    collection = db.dbs["tracking"]["announced"]
    await _retry_pending_announcement_deletes()
    await _retry_superseded_episode_deletes()
    cursor = collection.find(
        {
            "_id": {"$regex": r"^tv:\d+:episode:"},
            "superseded_by": {"$exists": False},
        },
        {"_id": 1, "tmdb_id": 1, "season_number": 1},
    )
    rows = await cursor.to_list(None)
    pending = set()
    for row in rows:
        match = re.match(r"^tv:(\d+):episode:(\d+):", str(row.get("_id") or ""))
        tmdb_id = _as_int(row.get("tmdb_id"), _as_int(match.group(1), 0) if match else 0)
        season_number = _as_int(
            row.get("season_number"), _as_int(match.group(2), 0) if match else 0
        )
        if tmdb_id and season_number:
            pending.add((tmdb_id, season_number))
    pending = {(tmdb_id, season) for tmdb_id, season in pending if tmdb_id > 0 and season > 0}

    docs_by_tmdb: dict[int, dict | None] = {}
    for tmdb_id, season_number in sorted(pending):
        if tmdb_id not in docs_by_tmdb:
            location = await db.find_media_doc("tv", tmdb_id)
            docs_by_tmdb[tmdb_id] = location[0] if location else None
        doc = docs_by_tmdb[tmdb_id]
        if not doc:
            continue
        info = _base_info_from_doc(doc, "tv")
        info.update({"media_type": "tv", "season_number": season_number})
        try:
            await _maybe_consolidate_season(info, doc, chat)
        except Exception as e:
            LOGGER.warning(
                f"Season reconciliation failed for tv={tmdb_id} season={season_number}: {e}"
            )


async def _announcement_reconciliation_loop() -> None:
    while True:
        try:
            await _reconcile_pending_seasons_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOGGER.warning(f"Announcement reconciliation loop failed: {e}", exc_info=True)
        await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)


def start_announcement_reconciler() -> None:
    """Start the idempotent hourly check for seasons waiting on air dates."""
    global _ANNOUNCEMENT_RECONCILER_TASK
    if _ANNOUNCEMENT_RECONCILER_TASK and not _ANNOUNCEMENT_RECONCILER_TASK.done():
        return
    try:
        _ANNOUNCEMENT_RECONCILER_TASK = asyncio.create_task(_announcement_reconciliation_loop())
    except RuntimeError:
        LOGGER.warning("Announcement reconciler not started: no running event loop.")


async def _announce(info: dict) -> None:
    settings = SettingsManager.current()
    chat = _resolve_chat(settings.announcement_channel)
    if not settings.announce_new_content or chat is None:
        return

    try:
        if info.get("media_type") == "movie":
            await _announce_movie(info, chat)
        elif info.get("media_type") == "tv":
            await _announce_episode(info, chat)
    except FloodWait as e:
        LOGGER.warning(f"Announcement FloodWait for {e.value}s")
    except Exception as e:
        LOGGER.error(f"Announcement failed for '{info.get('title')}': {e}", exc_info=True)


# Fire-and-forget announcement for a freshly added title / quality / episode.
def announce_new_media(info: dict) -> None:
    try:
        start_announcement_reconciler()
        asyncio.create_task(_announce(dict(info)))
    except RuntimeError:
        LOGGER.warning("Announcement skipped: no running event loop.")


async def delete_announcement(media_type: str, tmdb_id) -> None:
    """Delete every tracked post belonging to a movie or TV series."""
    if not tmdb_id:
        return
    collection = db.dbs["tracking"]["announced"]
    if media_type == "tv":
        pattern = f"^{re.escape(f'tv:{tmdb_id}')}(?::|$)"
        cursor = collection.find({"_id": {"$regex": pattern}})
    else:
        cursor = collection.find({"_id": f"movie:{tmdb_id}"})

    async for record in cursor:
        deleted = await _delete_tracked_announcement_messages(record)
        if deleted:
            await collection.delete_one({"_id": record["_id"]})


def delete_announcement_async(media_type: str, tmdb_id) -> None:
    try:
        asyncio.create_task(delete_announcement(media_type, tmdb_id))
    except RuntimeError:
        LOGGER.warning("Announcement delete skipped: no running event loop.")
