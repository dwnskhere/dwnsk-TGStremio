"""Tests for Telegraph-backed season episode/quality audit notifications."""
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
    "_build_missing_episode_telegraph_content",
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
        alerts = FakeCollection()
        pages = FakeCollection()
        self.page_records = pages
        self.db = SimpleNamespace(dbs={"tracking": {
            "season_missing_episode_alerts": alerts,
            "season_missing_episode_telegraph_pages": pages,
        }})
        self.pages = []
        settings = SimpleNamespace(current=lambda: SimpleNamespace(approver_ids=[101, 102]))

        async def create_or_update_page(tmdb_id, season_number, title, content, has_issues=None):
            self.pages.append({
                "tmdb_id": tmdb_id,
                "season_number": season_number,
                "title": title,
                "content": content,
                "has_issues": has_issues,
            })
            await self.page_records.update_one(
                {"_id": f"tv:{tmdb_id}:season:{season_number:02d}"},
                {"$set": {"has_open_issues": has_issues}},
                upsert=True,
            )
            return {
                "url": f"https://telegra.ph/Sample-Audit-{len(self.pages)}",
                "path": f"Sample-Audit-{len(self.pages)}",
                "title": title,
            }

        class FakeButton:
            def __init__(self, text, url):
                self.text, self.url = text, url

        class FakeMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard

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
            "_create_or_update_telegraph_audit_page": create_or_update_page,
            "InlineKeyboardMarkup": FakeMarkup,
            "InlineKeyboardButton": FakeButton,
            "LOGGER": FakeLogger(),
            "ParseMode": SimpleNamespace(HTML="HTML"),
        }
        exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.notify = namespace["_notify_approvers_missing_episodes"]
        self.audit = namespace["_build_season_quality_audit"]
        self.build_content = namespace["_build_missing_episode_telegraph_content"]

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
        season = {"episodes": [{"episode_number": 1, "telegram": [
            {"quality": "480p"}, {"quality": "540p"}, {"quality": "4K"}
        ]}]}
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

    def test_telegraph_content_uses_supported_rich_nodes_and_episode_sections(self):
        namespace = {}
        helper_nodes = [
            n for n in NODES
            if getattr(n, "name", "") in {"_as_int", "_build_missing_episode_telegraph_content"}
        ]
        exec(compile(ast.Module(body=helper_nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
        content = namespace["_build_missing_episode_telegraph_content"](
            {"tmdb_id": 123, "title": "Sample Show", "year": 2024},
            1,
            {"episode_numbers": {1, 2}, "aired_episodes": {
                1: {"name": "Episode One"}, 2: {"name": "Episode Two"}
            }},
            [1],
            {2: {"available_qualities": ["480p"], "missing_tiers": ["720p", "1080p"]}},
            1,
        )
        allowed_tags = {"a", "aside", "b", "blockquote", "br", "code", "em", "figcaption",
                        "figure", "h3", "h4", "hr", "i", "iframe", "img", "li", "ol", "p",
                        "pre", "s", "strong", "u", "ul", "video"}
        found_tags = set()
        text_content = []

        def walk(node):
            if isinstance(node, str):
                text_content.append(node)
                return
            found_tags.add(node["tag"])
            self.assertIn(node["tag"], allowed_tags)
            for child in node.get("children", []):
                walk(child)

        for node in content:
            walk(node)
        joined = " ".join(text_content)
        self.assertIn("S01E01 — Episode One", joined)
        self.assertIn("ALL MISSING", joined)
        self.assertIn("S01E02 — Episode Two", joined)
        self.assertIn("720p | 1080p", joined)
        self.assertIn("/tv/123", str(content))
        self.assertNotIn("table", found_tags)

    def test_telegraph_account_is_created_once_and_page_is_updated_in_place(self):
        import json
        from urllib.parse import quote

        accounts = FakeCollection()
        pages = FakeCollection()
        db_stub = SimpleNamespace(dbs={"tracking": {
            "telegraph_accounts": accounts,
            "season_missing_episode_telegraph_pages": pages,
        }})
        api_calls = []

        async def fake_api(method, payload):
            api_calls.append((method, payload))
            if method == "createAccount":
                return {"access_token": "test-access-token"}
            if method == "createPage":
                return {"url": "https://telegra.ph/Stable-Audit", "path": "Stable-Audit"}
            if method == f"editPage/{quote('Stable-Audit', safe='')}":
                return {"url": "https://telegra.ph/Stable-Audit", "path": "Stable-Audit"}
            raise AssertionError(f"Unexpected Telegraph API method: {method}")

        helper_names = {"_get_telegraph_access_token", "_create_or_update_telegraph_audit_page"}
        helper_nodes = [n for n in TREE.body if getattr(n, "name", "") in helper_names]
        namespace = {
            "db": db_stub,
            "_TELEGRAPH_ACCOUNT_LOCK": asyncio.Lock(),
            "_telegraph_api_request": fake_api,
            "datetime": datetime,
            "timezone": timezone,
            "json": json,
            "quote": quote,
            "LOGGER": FakeLogger(),
        }
        exec(compile(ast.Module(body=helper_nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)

        async def run():
            first = await namespace["_create_or_update_telegraph_audit_page"](
                321, 1, "Sample Show (2024)", [{"tag": "h3", "children": ["Audit"]}], True
            )
            second = await namespace["_create_or_update_telegraph_audit_page"](
                321, 1, "Sample Show (2024)", [{"tag": "h3", "children": ["Updated Audit"]}], True
            )
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first["url"], second["url"])
        self.assertEqual(len(api_calls), 3)  # one account, one create, one edit
        self.assertEqual(api_calls[1][0], "createPage")
        self.assertEqual(api_calls[2][0], "editPage/Stable-Audit")
        self.assertEqual(accounts.records["season_quality_audit"]["access_token"], "test-access-token")
        self.assertTrue(pages.records["tv:321:season:01"]["has_open_issues"])

    def test_existing_telegraph_page_is_marked_clear_when_audit_resolves(self):
        progress = {
            "aired_episodes": {1: {"name": "Episode One"}},
            "episode_numbers": {1},
        }
        info = {"tmdb_id": 654, "title": "Resolved Show", "year": 2024}

        async def run():
            await self.notify(info, 1, progress, {1}, 0, {})
            sent_before_resolution = len(FakeBot.sent)
            await self.notify(info, 1, progress, set(), 1, {})
            return sent_before_resolution

        sent_before_resolution = asyncio.run(run())
        self.assertEqual(sent_before_resolution, 2)
        self.assertEqual(len(FakeBot.sent), 2)  # resolution edits page, no separate DM
        self.assertEqual(len(self.pages), 2)
        self.assertFalse(self.pages[-1]["has_issues"])
        self.assertTrue(any("All audited episodes are complete" in str(node)
                            for node in self.pages[-1]["content"]))
        self.assertFalse(self.page_records.records["tv:654:season:01"]["has_open_issues"])

    def test_notifier_sends_short_telegram_link_and_deduplicates_state(self):
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
        self.assertEqual(len(FakeBot.sent), 4)  # two approvers for each of two audit states
        self.assertEqual(len(self.pages), 2)  # one shared Telegraph page create/update per changed state
        self.assertTrue(all(message["reply_markup"].inline_keyboard[0][0].text == "📄 Open full audit"
                            for message in FakeBot.sent))
        self.assertTrue(all("Sample Show (2024)" in message["text"] for message in FakeBot.sent))
        self.assertTrue(all("sendRichMessage" not in message["text"] for message in FakeBot.sent))
        page_nodes = self.pages[0]["content"]
        self.assertTrue(any(node.get("tag") == "h3" for node in page_nodes))


if __name__ == "__main__":
    unittest.main()
