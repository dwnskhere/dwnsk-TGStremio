"""Unit tests for approver notification formatting and duplicate suppression."""
from __future__ import annotations

import ast
import asyncio
import html
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "Backend/helper/announcer.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NODES = [
    node for node in TREE.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and node.name in {
        "_as_int",
        "_missing_episode_approver_ids",
        "_notify_approvers_missing_episodes",
    }
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


class FakeBot:
    sent = []

    @classmethod
    async def send_message(cls, **kwargs):
        cls.sent.append(kwargs)


class FakeLogger:
    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class MissingEpisodeNotifierTests(unittest.TestCase):
    def setUp(self):
        FakeBot.sent = []
        collection = FakeCollection()
        self.db = SimpleNamespace(
            dbs={"tracking": {"season_missing_episode_alerts": collection}}
        )
        settings = SimpleNamespace(current=lambda: SimpleNamespace(approver_ids=[101, 102]))
        namespace = {
            "SettingsManager": settings,
            "Telegram": SimpleNamespace(APPROVER_IDS=[], OWNER_ID=999),
            "_ANNOUNCEMENT_LOCKS": {},
            "asyncio": asyncio,
            "html": html,
            "datetime": datetime,
            "timezone": timezone,
            "db": self.db,
            "StreamBot": FakeBot,
            "LOGGER": FakeLogger(),
            "ParseMode": SimpleNamespace(HTML="HTML"),
        }
        exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.notify = namespace["_notify_approvers_missing_episodes"]

    def test_alert_contains_show_identity_and_missing_episode(self):
        progress = {
            "aired_episodes": {8: {"name": "The Final Door"}},
            "episode_numbers": {1, 2, 3, 4, 5, 6, 7, 8},
        }
        asyncio.run(self.notify(
            {"tmdb_id": 321, "title": "Sample Show", "year": 2024},
            1, progress, {8}, 7,
        ))

        self.assertEqual(len(FakeBot.sent), 2)  # one DM per configured approver
        text = FakeBot.sent[0]["text"]
        self.assertIn("Sample Show (2024)", text)
        self.assertIn("[TMDB ID: 321]", text)
        self.assertIn("Season 01", text)
        self.assertIn("S01E08", text)
        self.assertIn("The Final Door", text)
        self.assertIn("future episodes are excluded", text)

    def test_same_missing_list_does_not_send_duplicate_alerts(self):
        progress = {
            "aired_episodes": {8: {"name": "The Final Door"}},
            "episode_numbers": {1, 2, 3, 4, 5, 6, 7, 8},
        }
        args = ({"tmdb_id": 321, "title": "Sample Show", "year": 2024}, 1, progress)
        async def run():
            await self.notify(*args, {8}, 7)
            await self.notify(*args, {8}, 7)
            await self.notify(*args, {9}, 7)
        asyncio.run(run())
        self.assertEqual(len(FakeBot.sent), 4)  # first state + changed state, to two approvers


if __name__ == "__main__":
    unittest.main()
