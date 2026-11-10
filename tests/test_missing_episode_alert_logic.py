"""Focused source-level tests for the TMDb aired-episode filter.

Run with: python -m unittest tests.test_missing_episode_alert_logic
This test extracts the production function so it does not need live MongoDB,
Telegram credentials, or the full application dependency stack.
"""
from __future__ import annotations

import ast
import asyncio
import time
import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


SOURCE = Path(__file__).resolve().parents[1] / "Backend/helper/announcer.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
FUNCTION = next(
    node for node in TREE.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_get_tmdb_season_progress"
)


class DummyLogger:
    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class DummyResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class DummyAsyncClient:
    def __init__(self, responses, *args, **kwargs):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url, **_kwargs):
        return self.responses["show"] if "/tv/123" in url and "/season/" not in url else self.responses["season"]


class DummySemaphore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class DummyHTTPX:
    class Timeout:
        def __init__(self, *_args, **_kwargs):
            pass

    def __init__(self, responses):
        self.responses = responses

    def AsyncClient(self, **kwargs):
        return DummyAsyncClient(self.responses, **kwargs)


def as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class DummyTMDBProvider:
    API_SEMAPHORE = DummySemaphore()

    @staticmethod
    def tmdb_api_key():
        return "test-key"


class MissingEpisodeFilterTests(unittest.TestCase):
    def _load_function(self, episodes, expected_count=None):
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
        show = {
            "seasons": [{"season_number": 1, "episode_count": expected_count or len(episodes)}],
            "name": "Sample Show",
            "first_air_date": "2024-01-01",
        }
        season = {"episodes": episodes, "name": "Season 1"}
        namespace = {
            "_as_int": as_int,
            "_SEASON_PROGRESS_CACHE": {},
            "_SEASON_CACHE_TTL": 3600,
            "_SEASON_NEGATIVE_CACHE_TTL": 300,
            "time": time,
            "tmdb_provider": DummyTMDBProvider,
            "httpx": DummyHTTPX({"show": DummyResponse(show), "season": DummyResponse(season)}),
            "asyncio": asyncio,
            "LOGGER": DummyLogger(),
            "datetime": datetime,
            "timezone": timezone,
            "ZoneInfo": ZoneInfo,
            "date": date,
        }
        module = ast.Module(body=[FUNCTION], type_ignores=[])
        exec(compile(module, str(SOURCE), "exec"), namespace)
        return namespace["_get_tmdb_season_progress"]

    def test_only_already_aired_regular_episodes_are_reportable(self):
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        tomorrow = (today + timedelta(days=1)).isoformat()
        episodes = [
            {"episode_number": 1, "episode_type": "standard", "air_date": yesterday, "name": "Aired"},
            {"episode_number": 2, "episode_type": "standard", "air_date": tomorrow, "name": "Upcoming"},
            {"episode_number": 3, "episode_type": "special", "air_date": yesterday, "name": "Special"},
            {"episode_number": 4, "episode_type": "standard", "air_date": None, "name": "Unscheduled"},
            {"episode_number": 5, "episode_type": "standard", "air_date": today.isoformat(), "name": "Scheduled Later Today"},
        ]
        fn = self._load_function(episodes)
        progress = asyncio.run(fn(123, 1))
        self.assertEqual(progress["episode_numbers"], {1})
        self.assertFalse(progress["all_regular_episodes_aired"])
        self.assertEqual(progress["aired_episodes"][1]["name"], "Aired")

    def test_completed_season_metadata_is_marked_fully_aired(self):
        episodes = [
            {"episode_number": 1, "episode_type": "standard", "air_date": "2020-01-01", "name": "One"},
            {"episode_number": 2, "episode_type": "standard", "air_date": "2020-01-08", "name": "Two"},
            {"episode_number": 3, "episode_type": "special", "air_date": None, "name": "Special"},
        ]
        fn = self._load_function(episodes)
        progress = asyncio.run(fn(123, 1))
        self.assertEqual(progress["episode_numbers"], {1, 2})
        self.assertTrue(progress["all_regular_episodes_aired"])

    def test_partial_tmdb_metadata_is_not_used_for_alerts(self):
        episodes = [
            {"episode_number": 1, "episode_type": "standard", "air_date": "2020-01-01", "name": "One"},
        ]
        fn = self._load_function(episodes, expected_count=2)
        progress = asyncio.run(fn(123, 1))
        self.assertIsNone(progress)


if __name__ == "__main__":
    unittest.main()
