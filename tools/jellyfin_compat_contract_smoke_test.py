"""Isolated contract tests for Telegram-Stremio's Jellyfin compatibility API.

This uses fake MongoDB and a fake Telegram stream handler. It proves API
wiring/contracts only; real Telegram playback and Android-client compatibility
must still be checked against the deployed Heroku application.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import sys
import types
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient


class FakeObjectId:
    def __init__(self, value):
        self.value = str(value)

    def __str__(self):
        return self.value

    def __repr__(self):
        return self.value


bson = types.ModuleType("bson")
bson.ObjectId = FakeObjectId
sys.modules["bson"] = bson


def _matches(doc: dict, query: dict) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(doc, part) for part in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(doc, part) for part in expected):
                return False
            continue
        actual = doc.get(key)
        if isinstance(expected, dict):
            for op, value in expected.items():
                if op == "$exists":
                    if (key in doc) != bool(value):
                        return False
                elif op == "$in":
                    candidates = actual if isinstance(actual, list) else [actual]
                    if not any(candidate in value for candidate in candidates):
                        return False
                else:
                    return False
        elif isinstance(actual, list):
            if expected not in actual:
                return False
        elif str(actual) != str(expected):
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def __aiter__(self):
        self._it = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    def sort(self, key, direction=1):
        self.docs.sort(key=lambda item: item.get(key), reverse=direction < 0)
        return self

    def limit(self, amount):
        self.docs = self.docs[:amount]
        return self

    async def to_list(self, length=None):
        return self.docs[:length] if length is not None else self.docs


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(doc) for doc in (docs or [])]
        self._insert_counter = 0

    async def create_index(self, *args, **kwargs):
        return "ok"

    def find(self, query=None, projection=None):
        return FakeCursor([dict(doc) for doc in self.docs if _matches(doc, query or {})])

    async def find_one(self, query):
        for doc in self.docs:
            if _matches(doc, query):
                return dict(doc)
        return None

    async def insert_one(self, doc):
        self._insert_counter += 1
        copy = dict(doc)
        copy.setdefault("_id", f"session-{self._insert_counter}")
        self.docs.append(copy)

        class Result:
            inserted_id = copy["_id"]

        return Result()

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if _matches(doc, query):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                for key in update.get("$unset", {}):
                    doc.pop(key, None)
                return None
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)
        return None

    async def delete_one(self, query):
        for index, doc in enumerate(self.docs):
            if _matches(doc, query):
                del self.docs[index]
                break
        return None


movie_public = {
    "_id": "movie-object-1", "title": "Test Movie", "title_english": "Test Movie",
    "tmdb_id": 123, "imdb_id": "tt0000123", "release_year": 2024, "description": "A test movie",
    "genres": ["Drama", "Action"], "studios": "Studio One",
    "cast": [{"name": "Actor One", "character": "Lead"}],
    "rating": 7.5, "poster": "https://img.example/poster.jpg",
    "backdrop": "https://img.example/backdrop.jpg", "runtime": "1h 40m",
    "created_at": "2024-01-01T00:00:00Z",
    "telegram": [{"id": "encoded-movie-stream-id", "name": "Test.Movie.2024.1080p.mkv", "quality": "1080p", "size": "1 GB"}],
}
movie_owner = {
    "_id": "movie-owner", "title": "Owner Private", "tmdb_id": 124,
    "visibility": "owner", "release_year": 2024,
    "telegram": [{"id": "owner-private-id", "name": "owner.mkv", "quality": "1080p"}],
}
movie_other_token = {
    "_id": "movie-other-token", "title": "Other Token Private", "tmdb_id": 125,
    "visibility": "tokens", "allowed_tokens": ["someone-else"],
    "telegram": [{"id": "other-private-id", "name": "other.mkv", "quality": "1080p"}],
}
movie_allowed_token = {
    "_id": "movie-allowed-token", "title": "Allowed Token Movie", "tmdb_id": 126,
    "visibility": "tokens", "allowed_tokens": ["source-token"],
    "telegram": [{"id": "allowed-token-id", "name": "allowed.mkv", "quality": "720p"}],
}
movie_exclusive = {
    "_id": "movie-exclusive", "title": "Exclusive Catalog Movie", "tmdb_id": 127,
    "exclusive_catalog_id": "catalog-only",
    "telegram": [{"id": "exclusive-id", "name": "exclusive.mkv", "quality": "1080p"}],
}
tv = {
    "_id": "tv-object-1", "title": "Test Show", "tmdb_id": 456, "imdb_id": "tt0000456",
    "release_year": 2023, "description": "A test show",
    "poster": "https://img.example/show.jpg", "genres": ["Drama"], "studios": ["Studio TV"],
    "seasons": [{"season_number": 1, "episodes": [
        {
            "episode_number": 1, "title": "Pilot", "overview": "First episode",
            "released": "2024-01-01T00:00:00Z",
            "telegram": [{"id": "encoded-episode-stream-id", "name": "Test.Show.S01E01.mkv", "quality": "720p", "size": "500 MB"}],
        },
        {
            "episode_number": 2, "title": "Second Episode", "overview": "Second episode",
            "released": "2024-01-08T00:00:00Z",
            "telegram": [{"id": "encoded-episode-stream-id-2", "name": "Test.Show.S01E02.mkv", "quality": "720p", "size": "510 MB"}],
        },
        {
            "episode_number": 3, "title": "Third Episode", "overview": "Third episode",
            "released": "2024-01-15T00:00:00Z",
            "telegram": [{"id": "encoded-episode-stream-id-3", "name": "Test.Show.S01E03.mkv", "quality": "720p", "size": "520 MB"}],
        },
    ]}],
}

class FakeDatabase:
    def __init__(self, collections=None):
        self.collections = dict(collections or {})

    def __getitem__(self, key):
        return self.collections.setdefault(key, FakeCollection())

    async def command(self, name):
        assert name == "ping"
        return {"ok": 1}


dbs = {
    "tracking": FakeDatabase({
        "jellyfin_sessions": FakeCollection(),
        "jellyfin_playback_state": FakeCollection(),
        "jellyfin_display_preferences": FakeCollection(),
    }),
    "storage_1": FakeDatabase({
        "movie": FakeCollection([movie_public, movie_owner, movie_other_token, movie_allowed_token, movie_exclusive]),
        "tv": FakeCollection([tv]),
    }),
}
db_module = types.ModuleType("Backend.db")
db_module.dbs = dbs

Backend = types.ModuleType("Backend")
Backend.__path__ = []
Backend.db = db_module
sys.modules["Backend"] = Backend
sys.modules["Backend.db"] = db_module

config = types.ModuleType("Backend.config")


class Telegram:
    JELLYFIN_COMPAT_ENABLED = True
    JELLYFIN_USERNAME = "tester"
    JELLYFIN_PASSWORD = "very-secret"
    JELLYFIN_SOURCE_TOKEN = ""
    JELLYFIN_SERVER_ID = "server-id"
    JELLYFIN_SERVER_NAME = "Test Telegram Server"
    JELLYFIN_TOKEN_TTL_DAYS = 30
    OWNER_ID = 1


config.Telegram = Telegram
sys.modules["Backend.config"] = config

for name in ["Backend.fastapi", "Backend.fastapi.routes", "Backend.fastapi.security", "Backend.helper"]:
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module

stream_routes = types.ModuleType("Backend.fastapi.routes.stream_routes")


async def subtitle_handler(**kwargs):
    assert kwargs["token"] == "source-token"
    assert kwargs["id"] == "encoded-subtitle-record"
    assert kwargs["name"] == "Test.Movie.2024.English.srt"
    return Response(content=b"1\n00:00:01,000 --> 00:00:02,000\nHello\n", media_type="application/x-subrip")


async def stream_handler(**kwargs):
    request = kwargs["request"]
    assert request.path_params.get("name") == "Test.Movie.2024.1080p.mkv", request.path_params
    assert request.path_params.get("id") == "encoded-movie-stream-id", request.path_params
    assert request.headers.get("range") == "bytes=0-1023"
    assert kwargs["token"] == "source-token"
    assert kwargs["token_data"]["token"] == "source-token"
    return Response(
        content=b"stream-ok",
        status_code=206,
        media_type="video/x-matroska",
        headers={"Accept-Ranges": "bytes", "Content-Range": "bytes 0-8/9", "Content-Length": "9"},
    )


stream_routes.stream_handler = stream_handler
stream_routes.subtitle_handler = subtitle_handler
sys.modules[stream_routes.__name__] = stream_routes

subtitle_module = types.ModuleType("Backend.helper.subtitles")


async def get_subtitles_for(imdb_id, media_type, season, episode):
    if imdb_id == "tt0000123" and media_type == "movie":
        return [{
            "encoded": "encoded-subtitle-record",
            "name": "Test.Movie.2024.English.srt",
            "lang_code": "eng",
            "lang_label": "English",
            "chat_id": 100,
            "msg_id": 200,
        }]
    return []


def subtitle_ext(name):
    lower = (name or "").lower()
    for extension in (".srt", ".vtt", ".ass", ".ssa", ".sub"):
        if lower.endswith(extension):
            return extension
    return ".srt"


subtitle_module.get_subtitles_for = get_subtitles_for
subtitle_module.subtitle_ext = subtitle_ext
sys.modules[subtitle_module.__name__] = subtitle_module

tokens = types.ModuleType("Backend.fastapi.security.tokens")


async def verify_token(token):
    if token != "source-token":
        return None
    return {
        "name": "Source User",
        "token": token,
        "user_id": 99,
        "limits": {},
        "usage": {},
        "subscription_expired": False,
        "limit_exceeded": None,
    }


tokens.verify_token = verify_token
sys.modules[tokens.__name__] = tokens

settings_module = types.ModuleType("Backend.helper.settings_manager")


class Settings:
    base_url = "https://test.example"
    session_secret = "stable-secret"
    strm_default_token = "source-token"
    subscription = False


class SettingsManager:
    @staticmethod
    def current():
        return Settings()


settings_module.SettingsManager = SettingsManager
sys.modules[settings_module.__name__] = settings_module

strm = types.ModuleType("Backend.helper.strm_generator")


async def resolve_strm_token():
    return "source-token"


strm.resolve_strm_token = resolve_strm_token
sys.modules[strm.__name__] = strm

logger = types.ModuleType("Backend.logger")


class Logger:
    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


logger.LOGGER = Logger()
sys.modules[logger.__name__] = logger

repo_root = Path(__file__).resolve().parents[1]
route_path = repo_root / "Backend/fastapi/routes/jellyfin_routes.py"
spec = importlib.util.spec_from_file_location("Backend.fastapi.routes.jellyfin_routes", route_path)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

app = FastAPI()
app.include_router(mod.router)
openapi_schema = app.openapi()
assert "/System/Info/Public" in openapi_schema["paths"]
assert "/Items/{item_id}/PlaybackInfo" in openapi_schema["paths"]
client = TestClient(app)

# Anonymous, pre-login handshake: these calls must not require a Jellyfin token.
public_info = client.get("/System/Info/Public")
assert public_info.status_code == 200, public_info.text
assert public_info.json()["ProductName"] == "Jellyfin Server"
assert public_info.json()["Version"] == "10.10.7"
assert client.get("/system/info/public").status_code == 200
assert client.get("/System/Info").status_code == 200
assert client.get("/System/Endpoint").status_code == 200
health = client.get("/Jellyfin/Compat/Health")
assert health.status_code == 200 and health.json()["ContentSourceTokenConfigured"] is True
assert health.json()["ContentSourceTokenValid"] is True
assert health.json()["TrackingDatabaseReachable"] is True
assert health.json()["MediaStorageDatabasesReachable"] is True
# Web assets are injected by the Docker build; they are not in this isolated test checkout.
assert health.json()["WebClientAssetsPresent"] is False
assert health.json()["ReadyForAndroidClient"] is False
# Health must reject an invalid source token without exposing its value.
Telegram.JELLYFIN_SOURCE_TOKEN = "invalid-source-token"
mod._SOURCE_ACCESS_CACHE = None
invalid_health = client.get("/Jellyfin/Compat/Health")
assert invalid_health.status_code == 200
assert invalid_health.json()["ContentSourceTokenConfigured"] is True
assert invalid_health.json()["ContentSourceTokenValid"] is False
assert invalid_health.json()["ReadyForAndroidClient"] is False
Telegram.JELLYFIN_SOURCE_TOKEN = ""
mod._SOURCE_ACCESS_CACHE = None
assert client.get("/System/Configuration/Public").status_code == 200
assert client.get("/System/Ping").status_code == 200
assert client.post("/System/Ping").status_code == 200
assert client.post("/Sessions/Capabilities/Full", json={"PlayableMediaTypes": "Video"}).status_code == 204
assert client.post(
    "/Sessions/Capabilities/Full",
    json={"PlayableMediaTypes": "Video"},
    headers={"X-Emby-Token": "expired-token-from-previous-install"},
).status_code == 204
assert client.get("/Auth/Providers").json() == []
assert client.get("/Auth/PasswordResetProviders").json() == []
with client.websocket_connect("/socket") as websocket:
    assert websocket.receive_json()["MessageType"] == "KeepAlive"
    websocket.send_text('{"MessageType":"KeepAlive"}')
with client.websocket_connect("/socket?api_key=stale-or-placeholder-token") as websocket:
    assert websocket.receive_json()["MessageType"] == "KeepAlive"
print("PASS: public server discovery and anonymous bootstrap endpoints")
print("PASS: lowercase server-info path alias")
print("PASS: anonymous pre-login capability handshake and WebSocket keepalive")

# User/account login is separate from the Telegram-Stremio source token.
bad = client.post("/Users/AuthenticateByName", json={"Username": "tester", "Pw": "wrong-password"})
assert bad.status_code == 401, bad.text
login = client.post(
    "/Users/AuthenticateByName",
    json={"Username": "tester", "Pw": "very-secret"},
    headers={"Authorization": 'MediaBrowser Client="Test", Device="Phone", DeviceId="phone-1", Version="1.0"'},
)
assert login.status_code == 200, login.text
access = login.json()["AccessToken"]
assert access != "source-token"
auth = {"Authorization": f'MediaBrowser Token="{access}", Client="Test", Device="Phone", DeviceId="phone-1", Version="1.0"'}
assert login.json()["SessionInfo"]["Id"] == "session-1"
assert client.get("/Users/Me", headers=auth).status_code == 200
assert client.get("/Users/Public").json()[0]["Name"] == "tester"
assert client.get("/Sessions", headers=auth).status_code == 200
assert client.get("/System/Configuration", headers=auth).status_code == 200
with client.websocket_connect("/socket?api_key=" + access) as websocket:
    assert websocket.receive_json()["MessageType"] == "KeepAlive"
print("PASS: separate Jellyfin username/password login and opaque session token")
print("PASS: Jellyfin authorization header, authenticated routes and token-authenticated WebSocket")

# Display preferences are persisted per user/client.
user_id = mod._user_id()
prefs = client.get(f"/DisplayPreferences/usersettings?userId={user_id}&client=emby", headers=auth)
assert prefs.status_code == 200, prefs.text
assert prefs.json()["ViewType"] == "Poster"
assert client.post(
    f"/DisplayPreferences/usersettings?userId={user_id}&client=emby",
    headers=auth,
    json={"ViewType": "Thumb", "SortBy": "SortName"},
).status_code == 204
assert client.get(f"/DisplayPreferences/usersettings?userId={user_id}&client=emby", headers=auth).json()["ViewType"] == "Thumb"
print("PASS: display preferences read/write")

# Read-only library results enforce public/token-specific access and hide owner/exclusive content.
views = client.get("/UserViews", headers=auth)
assert views.status_code == 200, views.text
movies_view, shows_view = [item["Id"] for item in views.json()["Items"]]
root = client.get("/Items/Root", headers=auth)
assert root.status_code == 200 and str(uuid.UUID(root.json()["Id"])) == root.json()["Id"]
root_children = client.get("/Items", params={"ParentId": root.json()["Id"]}, headers=auth)
assert root_children.status_code == 200 and {item["Id"] for item in root_children.json()["Items"]} == {movies_view, shows_view}
assert client.get("/UserViews/GroupingOptions", headers=auth).json() == []
latest_global = client.get("/Items/Latest", headers=auth)
assert latest_global.status_code == 200 and isinstance(latest_global.json(), list), latest_global.text
movies = client.get("/Items", params={"ParentId": movies_view}, headers=auth)
assert movies.status_code == 200, movies.text
movie_names = {item["Name"] for item in movies.json()["Items"]}
assert movie_names == {"Test Movie", "Allowed Token Movie"}, movie_names
movie_id = next(item["Id"] for item in movies.json()["Items"] if item["Name"] == "Test Movie")
# Jellyfin declares item IDs as UUIDs. They must be stable and must not expose
# MongoDB IDs or the private source payload in the client-visible identifier.
assert str(uuid.UUID(movie_id)) == movie_id
assert "movie-object-1" not in movie_id
counts = client.get("/Items/Counts", headers=auth)
assert counts.status_code == 200 and counts.json()["MovieCount"] == 2, counts.text
filters_response = client.get("/Items/Filters", headers=auth)
assert filters_response.status_code == 200
assert set(filters_response.json()["Genres"]) >= {"Drama", "Action"}
assert "Studio One" in filters_response.json()["Studios"]
assert "Actor One" in filters_response.json()["Person"]
assert client.get("/Genres", headers=auth).status_code == 200
assert client.get("/Years", headers=auth).json()["TotalRecordCount"] >= 2
studios = client.get("/Studios", headers=auth)
assert studios.status_code == 200 and {item["Name"] for item in studios.json()["Items"]} >= {"Studio One", "Studio TV"}
persons = client.get("/Persons", headers=auth)
assert persons.status_code == 200 and any(item["Name"] == "Actor One" for item in persons.json()["Items"])
filtered = client.get("/Items", params={"ParentId": movies_view, "Genres": "Drama", "Years": "2024"}, headers=auth)
assert filtered.status_code == 200 and filtered.json()["TotalRecordCount"] == 1, filtered.text
# Match Jellyfin's documented multi-value filters (Genres/Studios may be pipe-delimited).
pipe_filtered = client.get("/Items", params={"ParentId": movies_view, "Genres": "Comedy|Drama", "Studios": "Studio One|Other"}, headers=auth)
assert pipe_filtered.status_code == 200 and {item["Name"] for item in pipe_filtered.json()["Items"]} == {"Test Movie"}, pipe_filtered.text
assert client.get("/Items", params={"ParentId": movies_view, "MediaTypes": "Audio"}, headers=auth).json()["TotalRecordCount"] == 0
assert client.get("/Items", params={"ParentId": movies_view, "MediaTypes": "Video"}, headers=auth).json()["TotalRecordCount"] == 2
assert client.get("/Items", params={"ParentId": movies_view, "ExcludeItemTypes": "Movie"}, headers=auth).json()["TotalRecordCount"] == 0
assert client.get("/Items", params={"ParentId": movies_view, "Person": "Actor One"}, headers=auth).json()["TotalRecordCount"] == 1
assert client.get("/Items", params={"ParentId": movies_view, "NameStartsWith": "Test"}, headers=auth).json()["TotalRecordCount"] == 1
assert client.get("/Items", params={"ParentId": movies_view, "MinCommunityRating": 8}, headers=auth).json()["TotalRecordCount"] == 0
assert client.get("/Users/" + user_id + "/Items", params={"ParentId": movies_view, "MediaTypes": "Video"}, headers=auth).json()["TotalRecordCount"] == 2
assert client.get("/Search/Hints?searchTerm=Test", headers=auth).status_code == 200
assert client.get(f"/Users/{user_id}/Views", headers=auth).status_code == 200
assert client.get(f"/Users/{user_id}/Configuration", headers=auth).status_code == 200
assert client.get("/QuickConnect/Enabled").json() is False
live_tv_info = client.get("/LiveTv/Info", headers=auth)
assert live_tv_info.status_code == 200 and live_tv_info.json() == {
    "IsEnabled": False, "Services": [], "EnabledUsers": [],
}, live_tv_info.text
assert client.get("/livetv/info", headers=auth).status_code == 200
print("PASS: movie/show views, documented filters, genres, years, studios, persons and search")
print("PASS: authenticated Live TV capability discovery reports unsupported service cleanly")
print("PASS: restricted, owner-only and exclusive catalog media are not leaked")

# Movie DTO and metadata mapping.
movie_detail_response = client.get("/Items/" + movie_id, headers=auth)
assert movie_detail_response.status_code == 200, movie_detail_response.text
movie_detail = movie_detail_response.json()
assert movie_detail["Name"] == "Test Movie"
assert movie_detail["RunTimeTicks"] == 100 * 60 * 10_000_000
assert mod._runtime_ticks("100 min") == 100 * 60 * 10_000_000
assert mod._runtime_ticks("1h 40m") == 100 * 60 * 10_000_000
assert client.get(f"/Items/{movie_id}/Images/Primary", headers=auth, follow_redirects=False).status_code == 307
image_infos = client.get(f"/Items/{movie_id}/Images", headers=auth)
assert image_infos.status_code == 200 and {item["ImageType"] for item in image_infos.json()} >= {"Primary", "Backdrop"}
assert client.get(f"/Items/{movie_id}/Similar", headers=auth).status_code == 200
# Simulate a Heroku dyno restart: rebuild UUID-to-source mappings from MongoDB.
mod._ITEM_PAYLOAD_CACHE.clear()
mod._CATALOG_CACHE = None
reloaded = client.get(f"/Items/{movie_id}", headers=auth)
assert reloaded.status_code == 200 and reloaded.json()["Name"] == "Test Movie", reloaded.text
print("PASS: movie detail, runtime conversion, poster redirect and similar items")

# TV show → season → episode and NextUp behavior.
shows = client.get("/Items", params={"ParentId": shows_view}, headers=auth)
assert shows.status_code == 200 and shows.json()["TotalRecordCount"] == 1, shows.text
show_id = shows.json()["Items"][0]["Id"]
seasons = client.get("/Shows/" + show_id + "/Seasons", headers=auth)
assert seasons.status_code == 200 and seasons.json()["TotalRecordCount"] == 1, seasons.text
season_id = seasons.json()["Items"][0]["Id"]
episodes = client.get("/Items", params={"ParentId": season_id}, headers=auth)
assert episodes.status_code == 200 and episodes.json()["TotalRecordCount"] == 3, episodes.text
episode_ids = [item["Id"] for item in episodes.json()["Items"]]
episode_id, episode2_id, episode3_id = episode_ids
next_up = client.get("/Shows/NextUp?UserId=" + user_id, headers=auth)
assert next_up.status_code == 200 and next_up.json()["TotalRecordCount"] == 1
assert next_up.json()["Items"][0]["Id"] == episode_id
assert client.get("/Shows/NextUp?UserId=" + user_id + "&NextUpDateCutoff=2025-01-01T00:00:00Z", headers=auth).json()["TotalRecordCount"] == 0
assert client.get("/Shows/NextUp?UserId=" + user_id + "&StartIndex=1", headers=auth).json()["StartIndex"] == 1
assert client.get("/Shows/Upcoming", headers=auth).status_code == 200
assert client.get(f"/Users/{user_id}/Items/Latest", headers=auth).status_code == 200
assert client.get("/Items/Latest", headers=auth).status_code == 200
assert client.get("/UserItems/Latest", headers=auth).status_code == 200
assert client.get("/UserItems/Resume?MediaTypes=Audio", headers=auth).json()["TotalRecordCount"] == 0
assert client.get("/Items/Root", headers=auth).status_code == 200
print("PASS: series → season → episode navigation, Items/Root and global latest-items contract")
print("PASS: NextUp filters, resumable preference, date cutoff and resume aliases")

# Playback state: resume takes priority for NextUp; disable resumable skips to the next unwatched episode.
assert client.post(f"/Users/{user_id}/PlayedItems/{episode_id}", headers=auth).status_code == 200
assert client.post(
    "/Sessions/Playing/Progress", headers=auth,
    json={"ItemId": episode2_id, "PlaybackPositionTicks": 50_000_000},
).status_code == 204
next_default = client.get("/Shows/NextUp?UserId=" + user_id + "&EnableResumable=true", headers=auth)
assert next_default.json()["Items"][0]["Id"] == episode2_id, next_default.text
next_no_resume = client.get("/Shows/NextUp?UserId=" + user_id + "&EnableResumable=false", headers=auth)
assert next_no_resume.json()["Items"][0]["Id"] == episode3_id, next_no_resume.text
resume = client.get(f"/Users/{user_id}/Items/Resume?MediaTypes=Video", headers=auth)
assert resume.status_code == 200 and resume.json()["TotalRecordCount"] == 1, resume.text
assert resume.json()["Items"][0]["UserData"]["PlaybackPositionTicks"] == 50_000_000
assert client.post(f"/Users/{user_id}/PlayedItems/{episode2_id}", headers=auth).status_code == 200
assert client.get("/Shows/NextUp?UserId=" + user_id, headers=auth).json()["Items"][0]["Id"] == episode3_id
assert client.post("/UserFavoriteItems/" + episode2_id, headers=auth).status_code == 200
assert client.get(f"/Users/{user_id}/FavoriteItems", headers=auth).json()["TotalRecordCount"] == 1
assert client.get(f"/Users/{user_id}/Items/{episode2_id}/UserData", headers=auth).json()["IsFavorite"] is True
assert client.post(
    "/Sessions/Playing/Stopped", headers=auth,
    json={"ItemId": episode2_id, "PlayedToCompletion": True},
).status_code == 204
print("PASS: playback progress, resume, NextUp ordering, played state and favorites")

# Playback negotiation and dispatch. Fake stream handler validates headers only.
playback = client.post("/Items/" + movie_id + "/PlaybackInfo", headers=auth)
assert playback.status_code == 200, playback.text
source = playback.json()["MediaSources"][0]
parts = urlsplit(source["Path"])
stream_token = parse_qs(parts.query).get("api_key", [None])[0]
assert stream_token and stream_token != access and "source-token" not in source["Path"]
assert dbs["tracking"]["jellyfin_playback_tickets"].find_one is not None
assert len(source["MediaStreams"]) == 1, source["MediaStreams"]
subtitle_track = source["MediaStreams"][0]
assert subtitle_track["Type"] == "Subtitle" and subtitle_track["Language"] == "eng"
assert subtitle_track["DeliveryMethod"] == "External"
subtitle_parts = urlsplit(subtitle_track["DeliveryUrl"])
subtitle_token = parse_qs(subtitle_parts.query).get("api_key", [None])[0]
assert subtitle_token and subtitle_token != access and "source-token" not in subtitle_track["DeliveryUrl"]
subtitle_response = client.get(subtitle_parts.path + "?" + subtitle_parts.query)
assert subtitle_response.status_code == 200 and b"Hello" in subtitle_response.content
stream = client.get(parts.path + "?" + parts.query, headers={"Range": "bytes=0-1023"})
assert stream.status_code == 206 and stream.content == b"stream-ok", stream.text
assert stream.headers.get("Accept-Ranges") == "bytes"
# Some Jellyfin clients include MediaSourceId in the stream URL path.
source_path_stream = client.get(
    f"/Videos/{movie_id}/{source['Id']}/stream?{parts.query}",
    headers={"Range": "bytes=0-1023"},
)
assert source_path_stream.status_code == 206, source_path_stream.text
assert client.get(
    f"/Videos/{movie_id}/invalid-media-source/stream?{parts.query}",
    headers={"Range": "bytes=0-1023"},
).status_code == 404
assert client.head(parts.path + "?" + parts.query, headers={"Range": "bytes=0-1023"}).status_code == 206
header_stream = client.get(
    f"/Videos/{movie_id}/stream",
    headers={**auth, "Range": "bytes=0-1023"},
)
assert header_stream.status_code == 206, header_stream.text
assert client.get(parts.path + "?api_key=wrong-ticket").status_code == 401
assert client.get(f"/Videos/{episode_id}/stream?api_key={stream_token}").status_code == 401
assert client.get(subtitle_parts.path + "?api_key=" + stream_token).status_code == 401
# An expired playback ticket is invalid even while its parent Jellyfin session
# remains active. MongoDB TTL cleanup is asynchronous, so route validation is
# responsible for enforcing expiry immediately.
ticket_hash = hashlib.sha256(stream_token.encode("utf-8")).hexdigest()
ticket_doc = next(
    doc for doc in dbs["tracking"]["jellyfin_playback_tickets"].docs
    if doc.get("token_hash") == ticket_hash
)
ticket_doc["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
assert client.get(parts.path + "?" + parts.query).status_code == 401
print("PASS: playback negotiation, scoped media tickets, external subtitles and Range forwarding")
print("NOTE: real Telegram byte streaming is not simulated by this contract test")

# Session revocation.
assert client.post("/Sessions/Logout", headers=auth).status_code == 204
assert client.get("/Users/Me", headers=auth).status_code == 401
assert client.get(parts.path + "?" + parts.query).status_code == 401
print("PASS: logout and revoked-token rejection")
assert mod.is_jellyfin_client_path("/Users/Me")
assert mod.is_jellyfin_client_path("/system/info/public")
assert mod.is_jellyfin_client_path("/DisplayPreferences/usersettings")
assert mod.is_jellyfin_client_path("/web/main.jellyfin.bundle.js")
assert not mod.is_jellyfin_client_path("/admin/dashboard")
print("PASS: Jellyfin unauthorized responses are classified as API paths, not admin-login redirects")

# Uvicorn can log WebSocket request targets, so query-string credentials must
# be redacted before a record is passed to a handler.
redaction_path = repo_root / "Backend/fastapi/log_redaction.py"
redaction_spec = importlib.util.spec_from_file_location("jellyfin_log_redaction", redaction_path)
redaction_module = importlib.util.module_from_spec(redaction_spec)
redaction_spec.loader.exec_module(redaction_module)
redaction_filter = redaction_module.RedactSensitiveQueryFilter()
record = logging.LogRecord(
    "uvicorn.error", logging.INFO, __file__, 1,
    "WebSocket /socket?api_key=%s&client=Android", ("super-secret-session-token",), None,
)
assert redaction_filter.filter(record)
assert "super-secret-session-token" not in record.getMessage()
assert "api_key=[REDACTED]" in record.getMessage()
print("PASS: WebSocket/query-string credentials are redacted from Uvicorn logs")
print("PASS: FastAPI OpenAPI schema and route registration")
print("ALL CONTRACT TESTS PASSED (real Heroku/Android/Telegram E2E still required)")
