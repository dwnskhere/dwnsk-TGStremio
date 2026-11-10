"""Tests for Rich Message season episode/quality audit notifications."""
from __future__ import annotations

import ast
import asyncio
import html
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "Backend/helper/announcer.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
WANTED = {
    "_as_int",
    "_quality_sort_key",
    "_quality_tiers_present",
    "_build_season_quality_audit",
    "_build_missing_episode_rich_message",
    "_missing_episode_approver_ids",
    "_notify_approvers_missing_episodes",
}
NODES = [
    node for node in TREE.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED
]


class FakeCollection:
    def __init__(self):
        self.records = {}

    async def find_one(self, query):
        return self.records.get(query["_id"])

    async def update_one(self, query, update, upsert=False):
        key = query["_id"]
        self.records[key] = {
            **self.records.get(key, {}),
            **update.get("$set", {}),
        }


class FakeBotAPI:
    calls = []

    @classmethod
    async def request(cls, method, payload):
        cls.calls.append({"method": method, "payload": payload})
        return {"message_id": len(cls.calls)}


class FakeBot:
    sent = []

    @classmethod
    async def send_message(cls, **kwargs):
        cls.sent.append(kwargs)


class FakeBotAPIError(Exception):
    pass


class FakeLogger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class MissingEpisodeNotifierTests(unittest.TestCase):
    def setUp(self):
        FakeBotAPI.calls = []
        FakeBot.sent = []
        collection = FakeCollection()
        self.db = SimpleNamespace(
            dbs={"tracking": {"season_missing_episode_alerts": collection}}
        )
        settings = SimpleNamespace(current=lambda: SimpleNamespace(approver_ids=[101, 102]))

        async def bot_api_request(method, payload):
            return await FakeBotAPI.request(method, payload)

        namespace = {
            "SettingsManager": settings,
            "Telegram": SimpleNamespace(APPROVER_IDS=[], OWNER_ID=999),
            "_ANNOUNCEMENT_LOCKS": {},
            "asyncio": asyncio,
            "datetime": datetime,
            "timezone": timezone,
            "html": html,
            "re": re,
            "db": self.db,
            "StreamBot": FakeBot,
            "_bot_api_request": bot_api_request,
            "_BotAPIError": FakeBotAPIError,
            "LOGGER": FakeLogger(),
            "ParseMode": SimpleNamespace(HTML="HTML"),
        }
        exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.notify = namespace["_notify_approvers_missing_episodes"]
        self.audit = namespace["_build_season_quality_audit"]

    def test_quality_tiers_are_independent_with_requested_substitutions(self):
        season = {"episodes": [
            {"episode_number": 1, "telegram": [{"quality": "1080p"}]},
            {"episode_number": 2, "telegram": [{"quality": "540p"}]},
            {"episode_number": 3, "telegram": [{"quality": "480p"}, {"quality": "720p"}]},
            {"episode_number": 4, "telegram": [{"quality": "480p"}, {"quality": "720p"}, {"quality": "2160p"}]},
        ]}
        result = self.audit(season, {1, 2, 3, 4})
        self.assertEqual(result["missing_episode_numbers"], set())
        self.assertEqual(result["indexed_count"], 4)
        self.assertEqual(result["quality_gaps"][1]["missing_tiers"], ["480p", "720p"])
        self.assertEqual(result["quality_gaps"][2]["missing_tiers"], ["480p", "1080p"])
        self.assertEqual(result["quality_gaps"][3]["missing_tiers"], ["1080p"])
        self.assertNotIn(4, result["quality_gaps"])

    def test_540p_substitutes_for_720p_and_higher_satisfies_1080p(self):
        season = {"episodes": [
            {"episode_number": 1, "telegram": [
                {"quality": "480p"}, {"quality": "540p"}, {"quality": "4K"}
            ]},
        ]}
        result = self.audit(season, {1})
        self.assertEqual(result["quality_gaps"], {})

    def test_missing_episode_and_dead_stream_are_all_missing(self):
        season = {"episodes": [
            {"episode_number": 2, "telegram": [{"quality": "1080p", "is_dead": True}]},
            {"episode_number": 3, "telegram": [{"quality": "2160p", "is_dead": True}]},
        ]}
        result = self.audit(season, {1, 2, 3})
        self.assertEqual(result["missing_episode_numbers"], {1, 2, 3})
        self.assertEqual(result["indexed_count"], 0)
        self.assertEqual(result["quality_gaps"], {})

    def test_rich_message_contains_native_three_column_table_and_tmdb_link(self):
        namespace = {}
        helper_nodes = [
            n for n in NODES
            if getattr(n, "name", "") in {"_as_int", "_build_missing_episode_rich_message"}
        ]
        exec(compile(ast.Module(body=helper_nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
        rich = namespace["_build_missing_episode_rich_message"](
            {"tmdb_id": 123, "title": "Sample Show", "year": 2024},
            1,
            {"episode_numbers": {1, 2}, "aired_episodes": {1: {"name": "Episode One"}, 2: {"name": "Episode Two"}}},
            [1],
            {2: {"available_qualities": ["480p"], "missing_tiers": ["720p", "1080p"]}},
            1,
        )
        blocks = rich["blocks"]
        table = next(block for block in blocks if block["type"] == "table")
        self.assertEqual(len(table["cells"][0]), 3)
        self.assertIn("ALL MISSING", table["cells"][1][2]["text"])
        self.assertIn("720p | 1080p", table["cells"][2][2]["text"])
        self.assertTrue(any(
            part.get("type") == "url" and part.get("url", "").endswith("/tv/123")
            for block in blocks if isinstance(block.get("text"), list)
            for part in block["text"] if isinstance(part, dict)
        ))

    def test_notifier_sends_rich_message_and_deduplicates_episode_and_quality_state(self):
        progress = {
            "aired_episodes": {8: {"name": "The Final Door"}, 9: {"name": "The New Threat"}},
            "episode_numbers": set(range(1, 10)),
        }
        args = ({"tmdb_id": 321, "title": "Sample Show", "year": 2024}, 1, progress)
        gaps = {9: {"available_qualities": ["480p"], "missing_tiers": ["720p", "1080p"]}}

        async def run():
            await self.notify(*args, {8}, 7, gaps)
            await self.notify(*args, {8}, 7, gaps)
            await self.notify(*args, {8}, 7, {
                9: {"available_qualities": ["480p", "720p"], "missing_tiers": ["1080p"]}
            })

        asyncio.run(run())
        self.assertEqual(len(FakeBotAPI.calls), 4)  # two changes for each of two approvers
        self.assertTrue(all(call["method"] == "sendRichMessage" for call in FakeBotAPI.calls))
        self.assertEqual(FakeBot.sent, [])
        first_payload = FakeBotAPI.calls[0]["payload"]["rich_message"]
        self.assertTrue(any(block["type"] == "table" for block in first_payload["blocks"]))


if __name__ == "__main__":
    unittest.main()
