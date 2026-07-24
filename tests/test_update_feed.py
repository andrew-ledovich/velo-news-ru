import json
import tempfile
import unittest
from pathlib import Path

from scripts.update_feed import build_feed, normalize_stories, save_json_atomic


class NormalizeStoriesTests(unittest.TestCase):
    def test_normalizes_valid_stories_and_sorts_newest_first(self):
        payload = {
            "stories": [
                {
                    "id": 10,
                    "headline": " Older headline ",
                    "source": None,
                    "priority": 2,
                    "summary": None,
                    "link": None,
                    "time": 1_700_000_000_000,
                    "effectiveTime": 1_700_000_100_000,
                    "effectivePrice": None,
                    "coins": [],
                },
                {
                    "id": 11,
                    "headline": "Newest headline",
                    "source": "FILING",
                    "priority": 1,
                    "summary": "Details",
                    "link": "https://example.com/story",
                    "time": 1_700_000_200_000,
                    "effectiveTime": 1_700_000_200_000,
                    "effectivePrice": 42.5,
                    "coins": ["BTC", "BTC", "ETH"],
                },
                {"id": 12, "headline": "", "time": 1_700_000_300_000},
            ]
        }

        stories = normalize_stories(payload)

        self.assertEqual([story["id"] for story in stories], [11, 10])
        self.assertEqual(stories[0]["titleEn"], "Newest headline")
        self.assertEqual(stories[0]["source"], "FILING")
        self.assertEqual(stories[0]["coins"], ["BTC", "ETH"])
        self.assertEqual(stories[0]["link"], "https://example.com/story")
        self.assertEqual(stories[1]["source"], "Velo")
        self.assertIsNone(stories[1]["link"])

    def test_rejects_payload_without_story_list(self):
        with self.assertRaises(ValueError):
            normalize_stories({"stories": "not-a-list"})


class BuildFeedTests(unittest.TestCase):
    def test_reuses_cached_translation_and_translates_only_new_titles(self):
        stories = [
            {"id": 2, "titleEn": "New title", "time": 2000},
            {"id": 1, "titleEn": "Cached title", "time": 1000},
        ]
        existing = {
            "items": [
                {
                    "id": 1,
                    "titleEn": "Cached title",
                    "titleRu": "Заголовок из кеша",
                    "translationStatus": "translated",
                }
            ]
        }
        translated = []

        def translate(text):
            translated.append(text)
            return "Новый заголовок"

        feed = build_feed(
            stories,
            existing_feed=existing,
            translate_fn=translate,
            generated_at="2026-07-24T14:00:00Z",
        )

        self.assertEqual(translated, ["New title"])
        self.assertEqual(feed["items"][0]["titleRu"], "Новый заголовок")
        self.assertEqual(feed["items"][1]["titleRu"], "Заголовок из кеша")
        self.assertEqual(feed["generatedAt"], "2026-07-24T14:00:00Z")
        self.assertEqual(feed["count"], 2)

    def test_translation_failure_keeps_original_and_marks_failure(self):
        stories = [{"id": 7, "titleEn": "Keep me visible", "time": 7000}]

        def fail(_text):
            raise RuntimeError("translator unavailable")

        feed = build_feed(stories, existing_feed={}, translate_fn=fail)

        self.assertEqual(feed["items"][0]["titleRu"], "Keep me visible")
        self.assertEqual(feed["items"][0]["translationStatus"], "failed")


class AtomicWriteTests(unittest.TestCase):
    def test_writes_valid_json_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data" / "news.json"
            save_json_atomic(target, {"count": 1, "items": [{"id": 1}]})

            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["count"], 1)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
