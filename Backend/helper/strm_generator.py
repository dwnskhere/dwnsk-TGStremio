"""
STRM and NFO library export engine for Emby, Jellyfin, and Kodi.
Transforms Telestream database entries into standard .strm (stream pointer) files
and companion .nfo metadata files.

Works in two modes:
  1. Local disk export (VPS co-located library directory for Emby).
  2. Zip archive stream for remote download / external Emby servers.
"""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import quote

from Backend import db
from Backend.helper.cf_stream import cf_enabled, cf_stream_url
from Backend.helper.nfo_generator import episode_nfo, movie_nfo, season_nfo, tvshow_nfo
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.webdav_fs import (
    fs,
    movie_folder_name,
    pick_best_quality,
    quality_ext,
    safe_name,
    show_folder_name,
)
from Backend.logger import LOGGER


def build_strm_url(stream_id: str, filename: str, token: str) -> str:
    """Construct a stream URL honoring Cloudflare Worker/proxy settings."""
    settings = SettingsManager.current()
    base_url = (settings.base_url or "").rstrip("/")
    quoted_name = quote(filename or "video.mkv")

    if cf_enabled() and settings.cf_stream_mode == "cloudflare":
        cf_url = cf_stream_url(token, stream_id, quoted_name)
        if cf_url:
            return cf_url

    raw_url = f"{base_url}/dl/{token}/{stream_id}/{quoted_name}"
    if settings.http_proxy_url:
        proxy = settings.http_proxy_url.rstrip("/")
        if settings.mediaflow_proxy:
            url = f"{proxy}/proxy/stream?d={quote(raw_url, safe='')}"
            if settings.mediaflow_password:
                url += f"&api_password={quote(settings.mediaflow_password, safe='')}"
            return url
        return f"{proxy}{raw_url}"

    return raw_url


async def resolve_strm_token(provided_token: Optional[str] = None) -> str:
    """Determine which token to embed inside .strm files."""
    if provided_token and provided_token.strip():
        return provided_token.strip()

    settings = SettingsManager.current()
    if settings.strm_default_token:
        return settings.strm_default_token

    # Fallback to the first active token in the database
    try:
        tokens = await db.get_all_api_tokens()
        if tokens:
            for t in tokens:
                tok = t.get("token") or t.get("_id")
                if tok and not t.get("revoked"):
                    return str(tok)
    except Exception as e:
        LOGGER.warning(f"[STRM] Failed to auto-resolve token from database: {e}")

    return "public"


class STRMExporter:
    """Generates .strm and .nfo tree structure."""

    def __init__(self, token: str, include_nfo: bool = True):
        self.token = token
        self.include_nfo = include_nfo

    async def collect_files(self) -> AsyncGenerator[tuple[str, str | bytes], None]:
        """
        Yields (relative_path, content) for all media in Telestream.
        Content is either a string (for .strm URLs) or bytes/str (for .nfo).
        """
        storage_keys = sorted(
            [k for k in db.dbs.keys() if k.startswith("storage_")],
            key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
        )

        # 1. Movies
        for db_key in storage_keys:
            db_conn = db.dbs[db_key]
            try:
                cursor = db_conn["movie"].find({})
                async for doc in cursor:
                    qualities = doc.get("telegram") or []
                    qual = pick_best_quality(qualities)
                    if not qual or not qual.get("id"):
                        continue

                    m_folder = movie_folder_name(doc)
                    title = doc.get("title_english") or doc.get("title") or "Unknown"
                    year = doc.get("release_year")
                    fname_base = f"{safe_name(title)}" + (f" ({year})" if year else "")
                    ext = quality_ext(qual.get("name") or "")

                    # .strm file
                    strm_url = build_strm_url(qual["id"], f"{fname_base}{ext}", self.token)
                    rel_strm = f"Movies/{m_folder}/{fname_base}.strm"
                    yield (rel_strm, f"{strm_url}\n")

                    # .nfo file
                    if self.include_nfo:
                        try:
                            nfo_content = movie_nfo(doc)
                            yield (f"Movies/{m_folder}/{fname_base}.nfo", nfo_content)
                        except Exception as err:
                            LOGGER.debug(f"[STRM] Error creating movie NFO for {m_folder}: {err}")
            except Exception as e:
                LOGGER.error(f"[STRM] Error scanning movies in db {db_idx}: {e}")

        # 2. TV Shows
        for db_key in storage_keys:
            db_conn = db.dbs[db_key]
            try:
                cursor = db_conn["tv"].find({})
                async for doc in cursor:
                    seasons = doc.get("seasons") or []
                    if not seasons:
                        continue

                    s_folder = show_folder_name(doc)
                    show_title = doc.get("title_english") or doc.get("title") or "Unknown"
                    show_short = safe_name(show_title, 60)

                    # Show NFO
                    if self.include_nfo:
                        try:
                            yield (f"TV Shows/{s_folder}/tvshow.nfo", tvshow_nfo(doc))
                        except Exception as err:
                            LOGGER.debug(f"[STRM] Error creating tvshow NFO for {s_folder}: {err}")

                    for s in seasons:
                        sn = s.get("season_number") or 0
                        episodes = s.get("episodes") or []
                        if not episodes:
                            continue

                        season_dir = f"TV Shows/{s_folder}/Season {sn:02d}"

                        # Season NFO
                        if self.include_nfo:
                            try:
                                yield (f"{season_dir}/season.nfo", season_nfo(doc, s))
                            except Exception as err:
                                LOGGER.debug(f"[STRM] Error creating season NFO for {season_dir}: {err}")

                        for ep in episodes:
                            en = ep.get("episode_number") or 0
                            ep_quals = ep.get("telegram") or []
                            qual = pick_best_quality(ep_quals)
                            if not qual or not qual.get("id"):
                                continue

                            ep_title = safe_name(ep.get("title") or f"Episode {en}", 60)
                            qlabel = safe_name(str(qual.get("quality") or "HD"), 20)
                            base_ep = f"{show_short} S{sn:02d}E{en:02d} - {ep_title}"
                            ext = quality_ext(qual.get("name") or "")

                            strm_url = build_strm_url(qual["id"], f"{base_ep} - {qlabel}{ext}", self.token)
                            yield (f"{season_dir}/{base_ep}.strm", f"{strm_url}\n")

                            if self.include_nfo:
                                try:
                                    yield (f"{season_dir}/{base_ep}.nfo", episode_nfo(doc, s, ep))
                                except Exception as err:
                                    LOGGER.debug(f"[STRM] Error creating episode NFO for {base_ep}: {err}")
            except Exception as e:
                LOGGER.error(f"[STRM] Error scanning series in db {db_idx}: {e}")

    async def export_to_directory(self, target_dir: str) -> dict:
        """Writes all files directly to local disk (useful for co-located Emby)."""
        target_dir = os.path.abspath(target_dir)
        os.makedirs(target_dir, exist_ok=True)

        count_strm = 0
        count_nfo = 0

        async for rel_path, content in self.collect_files():
            full_path = os.path.join(target_dir, rel_path.replace("/", os.sep))
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

            if isinstance(content, str):
                encoded = content.encode("utf-8")
            else:
                encoded = content

            # Only write if file doesn't exist or content changed to save disk I/O
            should_write = True
            if os.path.exists(full_path):
                try:
                    if os.path.getsize(full_path) == len(encoded):
                        with open(full_path, "rb") as cur:
                            if cur.read() == encoded:
                                should_write = False
                except Exception:
                    pass

            if should_write:
                with open(full_path, "wb") as f:
                    f.write(encoded)

            if rel_path.endswith(".strm"):
                count_strm += 1
            else:
                count_nfo += 1

        LOGGER.info(f"[STRM] Exported {count_strm} .strm files and {count_nfo} .nfo files to {target_dir}")
        return {
            "status": "success",
            "target_dir": target_dir,
            "strm_count": count_strm,
            "nfo_count": count_nfo,
        }

    async def create_zip_archive(self) -> io.BytesIO:
        """Bundles all .strm and .nfo files into an in-memory zip archive."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            async for rel_path, content in self.collect_files():
                if isinstance(content, str):
                    data = content.encode("utf-8")
                else:
                    data = content
                zf.writestr(rel_path, data)
        buf.seek(0)
        return buf

async def strm_auto_sync_loop(interval_hours: int = 24):
    """
    Background worker that runs every `interval_hours` (default: 24h)
    and automatically syncs the STRM library if an export directory is configured.
    """
    # Initial delay on boot to let Telegram clients and DB connections warm up
    await asyncio.sleep(60)

    while True:
        try:
            settings = SettingsManager.current()
            export_dir = settings.strm_export_dir
            if export_dir and os.path.isdir(export_dir):
                LOGGER.info("[STRM Auto-Sync] Running scheduled STRM library sync...")
                token = await resolve_strm_token()
                exporter = STRMExporter(token=token, include_nfo=True)
                res = await exporter.export_to_directory(export_dir)
                LOGGER.info(
                    f"[STRM Auto-Sync] Completed: {res.get('strm_count', 0)} streams, "
                    f"{res.get('nfo_count', 0)} metadata files."
                )
        except Exception as e:
            LOGGER.error(f"[STRM Auto-Sync] Scheduled sync encountered an error: {e}")

        # Wait for the next sync cycle (e.g. 24 hours)
        await asyncio.sleep(max(1, interval_hours) * 3600)

async def notify_emby_library_refresh():
    """Optionally instruct Emby Server to scan its library immediately via REST API."""
    settings = SettingsManager.current()
    api_key = settings.emby_api_key
    if not api_key:
        return
    emby_base = (settings.emby_url or "http://localhost:8096").rstrip("/")
    refresh_url = f"{emby_base}/Library/Refresh"
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            headers = {"X-Emby-Token": api_key}
            async with session.post(refresh_url, headers=headers, timeout=5) as resp:
                if resp.status in (200, 204):
                    LOGGER.info("[STRM] Triggered Emby library refresh successfully.")
    except Exception as e:
        LOGGER.debug(f"[STRM] Emby refresh notification failed (optional): {e}")


async def sync_single_media_strm(media_type: str, tmdb_id: int):
    """
    Instantly generates the .strm and .nfo files for newly forwarded media
    and saves them to STRM_EXPORT_DIR immediately, then pings Emby to refresh.
    """
    settings = SettingsManager.current()
    export_dir = settings.strm_export_dir
    if not export_dir or not os.path.isdir(export_dir):
        return

    try:
        location = await db.find_media_doc(media_type, tmdb_id)
        if not location:
            return
        doc, _ = location
        token = await resolve_strm_token()
        exporter = STRMExporter(token=token, include_nfo=True)

        items_to_write = []

        if media_type == "movie":
            qualities = doc.get("telegram") or []
            qual = pick_best_quality(qualities)
            if qual and qual.get("id"):
                m_folder = movie_folder_name(doc)
                title = doc.get("title_english") or doc.get("title") or "Unknown"
                year = doc.get("release_year")
                fname_base = f"{safe_name(title)}" + (f" ({year})" if year else "")
                ext = quality_ext(qual.get("name") or "")

                strm_url = build_strm_url(qual["id"], f"{fname_base}{ext}", token)
                items_to_write.append((f"Movies/{m_folder}/{fname_base}.strm", f"{strm_url}\n"))
                try:
                    items_to_write.append((f"Movies/{m_folder}/{fname_base}.nfo", movie_nfo(doc)))
                except Exception:
                    pass

        elif media_type in ("tv", "series"):
            seasons = doc.get("seasons") or []
            s_folder = show_folder_name(doc)
            show_title = doc.get("title_english") or doc.get("title") or "Unknown"
            show_short = safe_name(show_title, 60)

            try:
                items_to_write.append((f"TV Shows/{s_folder}/tvshow.nfo", tvshow_nfo(doc)))
            except Exception:
                pass

            for s in seasons:
                sn = s.get("season_number") or 0
                season_dir = f"TV Shows/{s_folder}/Season {sn:02d}"
                try:
                    items_to_write.append((f"{season_dir}/season.nfo", season_nfo(doc, s)))
                except Exception:
                    pass

                for ep in (s.get("episodes") or []):
                    en = ep.get("episode_number") or 0
                    qual = pick_best_quality(ep.get("telegram") or [])
                    if not qual or not qual.get("id"):
                        continue
                    ep_title = safe_name(ep.get("title") or f"Episode {en}", 60)
                    qlabel = safe_name(str(qual.get("quality") or "HD"), 20)
                    base_ep = f"{show_short} S{sn:02d}E{en:02d} - {ep_title}"
                    ext = quality_ext(qual.get("name") or "")

                    strm_url = build_strm_url(qual["id"], f"{base_ep} - {qlabel}{ext}", token)
                    items_to_write.append((f"{season_dir}/{base_ep}.strm", f"{strm_url}\n"))
                    try:
                        items_to_write.append((f"{season_dir}/{base_ep}.nfo", episode_nfo(doc, s, ep)))
                    except Exception:
                        pass

        for rel_path, content in items_to_write:
            full_path = os.path.join(export_dir, rel_path.replace("/", os.sep))
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            encoded = content.encode("utf-8") if isinstance(content, str) else content
            with open(full_path, "wb") as f:
                f.write(encoded)

        LOGGER.info(f"[STRM] Instant-synced {len(items_to_write)} file(s) for {media_type} (TMDB {tmdb_id})")
        await notify_emby_library_refresh()

    except Exception as e:
        LOGGER.error(f"[STRM] Instant sync failed for {media_type} {tmdb_id}: {e}")
