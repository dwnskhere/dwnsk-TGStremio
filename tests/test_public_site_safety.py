"""Source-level tests for public website projections and search helpers.

Run with: python -m unittest tests.test_public_site_safety
The tests extract pure helpers from the production module so they do not need
MongoDB, Telegram credentials, or the complete FastAPI application stack.
"""
from __future__ import annotations

import ast
import asyncio
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "Backend/fastapi/routes/public_site.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
WANTED = {
    "_query", "_safe_timestamp", "_safe_number", "_active_items", "_quality_labels",
    "_parsed_name", "_is_4k", "_is_recent_episode", "_card", "_public_file_info",
    "_safe_detail", "_search_clause", "_safe_telegram_link",
    "_candidate_limit", "_public_plan_record", "_get_public_document",
}
NODES = [
    node for node in TREE.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED
]


def parse_filename(name: str) -> dict:
    return {
        "resolution": "2160p" if "2160" in name else "1080p",
        "quality": "WEB-DL",
        "encode": "x265",
        "visualTags": ["HDR10"],
        "audioTags": ["DDP"],
        "audioChannels": ["5.1"],
        "languages": ["English"],
        "container": "mkv",
    }


NAMESPACE = {
    "re": re,
    "datetime": datetime,
    "timedelta": timedelta,
    "timezone": timezone,
    "Any": object,
    "parse_filename": parse_filename,
    "resolve_cover_url": lambda value: value or "",
    "_PUBLIC_LIST_MAX_CANDIDATES": 30_000,
    "_PLAN_LABELS": {30: "Starter", 90: "Basic", 180: "Pro", 365: "Premium"},
    "_MANUAL_DAILY_LIMITS_GB": {30: 50, 90: 70, 180: 98, 365: 137},
    "_DETAIL_MOVIE_PROJECTION": {"tmdb_id": 1, "telegram.quality": 1},
    "_DETAIL_TV_PROJECTION": {"tmdb_id": 1, "seasons.episodes.telegram.quality": 1},
}
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), "exec"), NAMESPACE)


class PublicSiteSafetyTests(unittest.TestCase):
    def test_public_home_and_admin_home_have_separate_access_boundaries(self):
        main_source = (Path(__file__).resolve().parents[1] / "Backend/fastapi/main.py").read_text(encoding="utf-8")
        main_tree = ast.parse(main_source)
        routes = {}
        for node in main_tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            decorators = [d for d in node.decorator_list if isinstance(d, ast.Call)]
            for decorator in decorators:
                if isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "get":
                    if decorator.args and isinstance(decorator.args[0], ast.Constant):
                        routes[decorator.args[0].value] = node
        self.assertIn("/", routes)
        self.assertIn("/admin", routes)
        root_args = [arg.arg for arg in routes["/"].args.args]
        self.assertEqual(root_args, ["request"])
        admin_source = ast.unparse(routes["/admin"])
        self.assertIn("Depends(require_auth)", admin_source)
        self.assertIn("include_router(public_site_router)", main_source)

    def test_detail_lookup_does_not_fall_back_to_older_public_duplicate(self):
        class FakeCollection:
            def __init__(self, doc):
                self.doc = doc

            async def find_one(self, query, projection):
                return self.doc

        class FakeStorage:
            def __init__(self, doc):
                self.movie = FakeCollection(doc)
                self.tv = FakeCollection(doc)

            def __getitem__(self, collection):
                return getattr(self, collection)

        original_storage_items = NAMESPACE.get("_storage_items")
        NAMESPACE["_storage_items"] = lambda: [
            (1, FakeStorage({"tmdb_id": 42, "visibility": "public", "telegram": [{"is_dead": False}]})),
            (2, FakeStorage({"tmdb_id": 42, "visibility": "owner", "telegram": [{"is_dead": False}]})),
        ]
        try:
            result = asyncio.run(NAMESPACE["_get_public_document"]("movie", 42))
            self.assertIsNone(result)
        finally:
            if original_storage_items is None:
                NAMESPACE.pop("_storage_items", None)
            else:
                NAMESPACE["_storage_items"] = original_storage_items


    def test_public_visibility_excludes_token_owner_and_exclusive_catalog_records(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        values = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"_PUBLIC_VISIBILITY", "_PUBLIC_CATALOG"}
        }
        visibility = values["_PUBLIC_VISIBILITY"]
        catalog = values["_PUBLIC_CATALOG"]
        self.assertEqual(visibility["$or"], [
            {"visibility": "public"}, {"visibility": {"$exists": False}}
        ])
        self.assertEqual(catalog["$or"], [
            {"exclusive_catalog_id": {"$exists": False}}, {"exclusive_catalog_id": None}
        ])

    def test_telegram_links_are_normalized_and_restricted(self):
        safe = NAMESPACE["_safe_telegram_link"]
        self.assertEqual(safe("@CodecIndexbot"), "https://t.me/CodecIndexbot")
        self.assertEqual(safe("https://t.me/CodecIndexbot"), "https://t.me/CodecIndexbot")
        self.assertEqual(safe("https://t.me/CodecIndexbot?start=private"), "")
        self.assertEqual(safe("javascript:alert(1)"), "")

    def test_search_supports_imdb_tmdb_and_title_year(self):
        search = NAMESPACE["_search_clause"]
        self.assertIn("imdb_id", search("tt1234567"))
        self.assertEqual(search("12345"), {"tmdb_id": 12345})
        self.assertEqual(search("The Odyssey (2026)")["$and"][1], {"release_year": 2026})

    def test_file_projection_never_returns_raw_filename_or_telegram_ids(self):
        item = {
            "quality": "2160p",
            "name": "Private.Title.2026.2160p.WEB-DL.x265.HDR10.DDP5.1.English.mkv",
            "id": "internal-stream-id",
            "size": "4.2 GB",
            "parts": [{"chat_id": -100123, "msg_id": 777, "part_number": 1}],
        }
        safe = NAMESPACE["_public_file_info"](item)
        serialized = repr(safe)
        for forbidden in ("name", "Private.Title", "internal-stream-id", "chat_id", "msg_id", "-100123", "777"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(safe["codec"], "x265")
        self.assertEqual(safe["container"], "MKV")
        self.assertEqual(safe["parts_count"], 1)

    def test_movie_detail_does_not_return_private_or_internal_fields(self):
        doc = {
            "_id": "mongo-object-id",
            "tmdb_id": 123,
            "imdb_id": "tt1234567",
            "visibility": "owner",
            "title": "Public title",
            "telegram": [{
                "quality": "1080p", "name": "Private.Filename.1080p.WEB-DL.mkv",
                "id": "internal-id", "size": "2 GB", "is_dead": False,
                "parts": [{"chat_id": 123, "msg_id": 456, "part_number": 1}],
            }],
        }
        safe = NAMESPACE["_safe_detail"](doc, "movie")
        text = repr(safe)
        for forbidden in ("mongo-object-id", "imdb_id", "visibility", "Private.Filename", "internal-id", "chat_id", "msg_id"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(len(safe["files"]), 1)

    def test_dead_streams_are_not_returned(self):
        doc = {"tmdb_id": 1, "title": "Example", "telegram": [
            {"quality": "720p", "is_dead": True, "name": "dead.mkv"},
            {"quality": "1080p", "is_dead": False, "name": "live.1080p.WEB-DL.mkv"},
        ]}
        safe = NAMESPACE["_safe_detail"](doc, "movie")
        self.assertEqual(len(safe["files"]), 1)
        self.assertEqual(safe["files"][0]["quality"], "1080p")

    def test_library_counts_distinct_quality_labels(self):
        doc = {
            "tmdb_id": 9, "title": "Example", "updated_on": datetime(2026, 11, 10),
            "telegram": [
                {"quality": "1080p", "name": "one.1080p.WEB-DL.mkv", "is_dead": False},
                {"quality": "1080p", "name": "two.1080p.WEB-DL.mkv", "is_dead": False},
                {"quality": "720p", "name": "three.720p.WEB-DL.mkv", "is_dead": False},
            ],
        }
        card = NAMESPACE["_card"](doc, "movie")
        self.assertEqual(card["file_count"], 2)
        self.assertEqual(card["quality_labels"], ["720p", "1080p"])
        self.assertGreater(card["_sort_ts"], 0)

    def test_4k_detection_uses_quality_and_parsed_resolution(self):
        is_4k = NAMESPACE["_is_4k"]
        self.assertTrue(is_4k([{"quality": "2160p", "is_dead": False}]))
        self.assertTrue(is_4k([{"quality": "Unknown", "name": "movie.2160p.WEB-DL.mkv", "is_dead": False}]))
        self.assertFalse(is_4k([{"quality": "2160p", "is_dead": True}]))

    def test_new_episode_uses_utc_indexing_timestamp_and_active_stream(self):
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        self.assertTrue(NAMESPACE["_is_recent_episode"]({
            "indexed_on": recent,
            "telegram": [{"quality": "720p", "is_dead": False}],
        }, datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)))
        self.assertFalse(NAMESPACE["_is_recent_episode"]({
            "indexed_on": datetime.now(timezone.utc) - timedelta(days=20),
            "telegram": [{"quality": "720p", "is_dead": False}],
        }, datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)))
        self.assertFalse(NAMESPACE["_is_recent_episode"]({
            "indexed_on": datetime.now(timezone.utc),
            "telegram": [{"quality": "720p", "is_dead": True}],
        }, datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)))

    def test_candidate_limit_allows_full_catalogue_pagination_with_a_hard_bound(self):
        candidate_limit = NAMESPACE["_candidate_limit"]
        self.assertEqual(candidate_limit(50, 24), 1200)
        self.assertEqual(candidate_limit(453, 24), 10_872)
        self.assertEqual(candidate_limit(500, 60), 30_000)
        self.assertEqual(candidate_limit(900, 60), 30_000)

    def test_public_plan_projection_is_database_driven_and_excludes_internal_fields(self):
        project = NAMESPACE["_public_plan_record"]
        plan = {
            "_id": "private-object-id", "name": "HD Plus", "days": 90,
            "price": 179, "currency": "INR", "created_at": "private",
            "api_token": "must-not-leak",
        }
        safe = project(plan)
        self.assertEqual(safe["name"], "HD Plus")
        self.assertEqual(safe["days"], 90)
        self.assertEqual(safe["price"], 179)
        self.assertEqual(safe["daily_limit_gb"], 70)
        self.assertNotIn("_id", safe)
        self.assertNotIn("created_at", safe)
        self.assertNotIn("api_token", safe)
        self.assertIsNone(project({"days": 0, "price": 10}))
        self.assertIsNone(project({"days": 30, "price": float("inf")}))


if __name__ == "__main__":
    unittest.main()
