"""Telegram channel announcements for newly indexed media.

Announcements are keyed per movie, episode, or completed season.  The public
post is a Telegram Rich Message (HTML + poster slideshow) sent through the Bot
API; Pyrogram remains the fallback for older Bot API deployments.
"""
from __future__ import annotations

import asyncio
import html
import re
import time
from datetime import date, datetime, timezone
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
_SEASON_CACHE_TTL = 60 * 60
_SEASON_NEGATIVE_CACHE_TTL = 5 * 60
_IMAGE_CACHE_TTL = 6 * 60 * 60
_RECONCILE_INTERVAL_SECONDS = 60 * 60
_SEASON_METADATA_CACHE: dict[tuple[int, int], tuple[float, dict | None]] = {}
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

    parts = [header, "", table]
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


async def _publish_or_update(key: str, chat, info: dict, season_metadata: dict | None = None) -> bool:
    """Create a post once, then edit the same Telegram message on later updates."""
    lock = _ANNOUNCEMENT_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        info = await _enrich_announcement_info(info)
        collection = db.dbs["tracking"]["announced"]
        record = await collection.find_one({"_id": key})
        poster_urls = await _get_posters(info, season_metadata)
        message_id = (record or {}).get("message_id")
        old_chat = (record or {}).get("chat_id") or chat

        if message_id:
            if await _edit_announcement(old_chat, message_id, info, poster_urls):
                update_fields = {
                    "chat_id": old_chat,
                    "message_id": int(message_id),
                    "updated_at": datetime.utcnow(),
                    "kind": info.get("announcement_kind"),
                    "tmdb_id": info.get("tmdb_id"),
                    "media_type": info.get("media_type"),
                    "season_number": info.get("season_number"),
                    "episode_number": info.get("episode_number"),
                }
                if season_metadata:
                    update_fields.update({
                        "episode_numbers": sorted(season_metadata.get("episode_numbers") or []),
                        "season_poster_path": season_metadata.get("poster_path") or "",
                        "season_overview": season_metadata.get("season_overview") or "",
                        "season_name": season_metadata.get("season_name") or "",
                    })
                await collection.update_one(
                    {"_id": key},
                    {"$set": update_fields},
                    upsert=True,
                )
                return True

        new_message_id = await _send_announcement(chat, info, poster_urls)
        if not new_message_id:
            return False

        update_fields = {
            "chat_id": chat,
            "message_id": int(new_message_id),
            "updated_at": datetime.utcnow(),
            "created_at": (record or {}).get("created_at") or datetime.utcnow(),
            "kind": info.get("announcement_kind"),
            "tmdb_id": info.get("tmdb_id"),
            "media_type": info.get("media_type"),
            "season_number": info.get("season_number"),
            "episode_number": info.get("episode_number"),
        }
        if season_metadata:
            update_fields.update({
                # Persist a JSON/BSON-safe list rather than a Python set.
                "episode_numbers": sorted(season_metadata.get("episode_numbers") or []),
                "season_poster_path": season_metadata.get("poster_path") or "",
                "season_overview": season_metadata.get("season_overview") or "",
                "season_name": season_metadata.get("season_name") or "",
            })
        await collection.update_one(
            {"_id": key},
            {"$set": update_fields},
            upsert=True,
        )
        if message_id and old_chat:
            await _delete_telegram_message(old_chat, message_id)
        return True


async def _get_tmdb_season_metadata(tmdb_id, season_number: int) -> dict | None:
    """Return a season's complete, already-aired TMDb episode list.

    Consolidation is deliberately conservative: missing API keys, incomplete
    metadata, missing air dates, and future-dated episodes all prevent it.
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

    api_key = tmdb_provider.tmdb_api_key()
    if not api_key:
        LOGGER.warning("Season consolidation is waiting: TMDb API key is not configured.")
        _SEASON_METADATA_CACHE[cache_key] = (time.monotonic(), None)
        return None

    base = "https://api.themoviedb.org/3"
    try:
        async with tmdb_provider.API_SEMAPHORE:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                show_response, season_response = await asyncio.gather(
                    client.get(f"{base}/tv/{tmdb_id_int}", params={"api_key": api_key, "language": "en-US"}),
                    client.get(f"{base}/tv/{tmdb_id_int}/season/{season_number}", params={"api_key": api_key, "language": "en-US"}),
                )
        if show_response.status_code != 200 or season_response.status_code != 200:
            LOGGER.debug(
                f"TMDb season metadata unavailable for tv={tmdb_id_int} season={season_number}: "
                f"show={show_response.status_code}, season={season_response.status_code}"
            )
            return None
        show_data = show_response.json()
        season_data = season_response.json()
        season_summary = next((
            item for item in (show_data.get("seasons") or [])
            if _as_int(item.get("season_number"), -1) == season_number
        ), None)
        expected_count = _as_int((season_summary or {}).get("episode_count"), 0)
        metadata_episodes = season_data.get("episodes") or []
        if expected_count <= 0 and metadata_episodes:
            # Some TMDb responses omit episode_count from the series season
            # summary while still returning the full season episode list.
            expected_count = len(metadata_episodes)

        # The metadata list must be complete and every regular episode must
        # have aired before it can be used as the consolidation checklist.
        episode_numbers = _released_regular_episode_numbers(metadata_episodes, expected_count)
        if not episode_numbers:
            _SEASON_METADATA_CACHE[cache_key] = (time.monotonic(), None)
            return None

        result = {
            "episode_numbers": episode_numbers,
            "season_name": str(season_data.get("name") or f"Season {season_number}"),
            "season_overview": str(season_data.get("overview") or ""),
            "poster_path": str(season_data.get("poster_path") or ""),
            "expected_count": expected_count,
        }
        _SEASON_METADATA_CACHE[cache_key] = (time.monotonic(), result)
        return result
    except Exception as e:
        LOGGER.warning(
            f"TMDb season completion check failed for tv={tmdb_id_int} "
            f"season={season_number} ({type(e).__name__})."
        )
        return None


def _build_episode_info(info: dict, doc: dict | None, season_number: int, episode_number: int) -> dict:
    result = _base_info_from_doc(doc, "tv") if doc else dict(info)
    result.update({
        "announcement_kind": "episode",
        "season_number": season_number,
        "episode_number": episode_number,
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
        "season_name": (season_metadata or {}).get("season_name") or f"Season {season_number}",
        "season_overview": (season_metadata or {}).get("season_overview") or "",
        "season_poster_path": (season_metadata or {}).get("poster_path") or "",
        "qualities": _season_qualities(season, episode_numbers),
    })
    return result


async def _remove_legacy_tv_announcement(tmdb_id) -> None:
    """Remove the old one-post-per-series tracking entry during migration."""
    key = f"tv:{tmdb_id}"
    collection = db.dbs["tracking"]["announced"]
    record = await collection.find_one({"_id": key})
    if not record:
        return
    if await _delete_telegram_message(record.get("chat_id"), record.get("message_id")):
        await collection.delete_one({"_id": key})


async def _cleanup_episode_announcements(tmdb_id, season_number: int, season_key: str) -> None:
    """Remove episode posts only after the consolidated season post exists."""
    collection = db.dbs["tracking"]["announced"]
    prefix = re.escape(f"tv:{tmdb_id}:episode:{season_number:02d}:")
    cursor = collection.find({"_id": {"$regex": f"^{prefix}"}})
    async for record in cursor:
        if await _delete_telegram_message(record.get("chat_id"), record.get("message_id")):
            await collection.update_one(
                {"_id": record["_id"]},
                {"$set": {"message_id": None, "superseded_by": season_key, "updated_at": datetime.utcnow()}},
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
        if await _delete_telegram_message(record.get("chat_id"), record.get("message_id")):
            await collection.update_one(
                {"_id": record["_id"]},
                {"$set": {"message_id": None, "updated_at": datetime.utcnow()}},
            )


async def _maybe_consolidate_season(info: dict, doc: dict, chat) -> bool:
    tmdb_id = info.get("tmdb_id")
    season_number = _as_int(info.get("season_number"), 0)
    if not tmdb_id or season_number <= 0:
        return False

    metadata = await _get_tmdb_season_metadata(tmdb_id, season_number)
    if not metadata:
        LOGGER.debug(
            f"Season announcement pending for tv={tmdb_id} S{season_number:02d}: "
            "TMDb has not returned a complete released-episode checklist yet."
        )
        return False

    season = _find_season(doc, season_number)
    if not season:
        LOGGER.warning(
            f"Season announcement cannot run for tv={tmdb_id} S{season_number:02d}: "
            "season is absent from the indexed media document."
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

    # Only remove episode posts after the season post has been confirmed.
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
        deleted = await _delete_telegram_message(record.get("chat_id"), record.get("message_id"))
        if deleted:
            await collection.delete_one({"_id": record["_id"]})


def delete_announcement_async(media_type: str, tmdb_id) -> None:
    try:
        asyncio.create_task(delete_announcement(media_type, tmdb_id))
    except RuntimeError:
        LOGGER.warning("Announcement delete skipped: no running event loop.")
