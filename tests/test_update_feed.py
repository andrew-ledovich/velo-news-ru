import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from scripts.update_feed import (
    _compile_topic_rules,
    _entry_categories,
    _entry_datetime,
    _entry_id,
    _translation_cache,
    build_feed,
    classify,
    load_sources,
    parse_entries,
    save_json_atomic,
)


SAMPLE_CONFIG = {
    "sources": [
        {
            "id": "techcrunch",
            "name": "TechCrunch",
            "feed": "https://example.com/tc",
            "enabled": True,
            "cap": 5,
        }
    ],
    "topics": [{"id": "ai", "label": "AI"}],
    "classification": {
        "ai": [r"\bai\b", r"\b(anthropic|openai|claude|gpt)\b"],
        "ev": [r"\b(ev|tesla)\b"],
        "crypto": [r"\b(bitcoin|btc|ethereum)\b"],
        "macro": [r".*"],
    },
    "settings": {"max_items": 200, "keep_days": 7},
}


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.rules = _compile_topic_rules(SAMPLE_CONFIG["classification"])

    def test_ai_wins_over_macro_for_openai_mention(self):
        self.assertEqual(
            classify("OpenAI launches new model", "", ["AI"], self.rules),
            "AI",
        )

    def test_ai_word_boundary_does_not_match_available(self):
        self.assertEqual(
            classify("Funds available for new partners", "", [], self.rules),
            "MACRO",
        )

    def test_ev_for_tesla(self):
        self.assertEqual(
            classify("Tesla deliveries drop", "", [], self.rules),
            "EV",
        )

    def test_crypto_for_bitcoin(self):
        self.assertEqual(
            classify("Bitcoin breaks $100k", "", ["Markets"], self.rules),
            "CRYPTO",
        )

    def test_macro_default_for_unrelated_news(self):
        self.assertEqual(
            classify("Central bank holds rates", "", [], self.rules),
            "MACRO",
        )

    def test_ai_priority_over_ev_when_both_keywords(self):
        # Order in yaml puts AI first; AI wins.
        self.assertEqual(
            classify("Tesla to deploy AI in factories", "", [], self.rules),
            "AI",
        )


class ParseEntriesTests(unittest.TestCase):
    def test_skips_entries_without_title_or_link(self):
        import feedparser

        raw = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<rss version="2.0"><channel>'
            b'<title>Feed</title>'
            b'<item><title>Hello</title></item>'
            b'<item><title>World</title><link>https://example.com/w</link></item>'
            b'</channel></rss>'
        )
        parsed = feedparser.parse(raw)
        since = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        out = parse_entries(parsed, "test", cap=10, category_filter=None, since=since)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["titleEn"], "World")
        self.assertTrue(out[0]["link"].startswith("https://"))

    def test_filters_by_category(self):
        import feedparser

        raw = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            b'<channel><title>F</title>'
            b'<item><title>Keep me</title><link>https://x/k</link>'
            b'<category>AI</category></item>'
            b'<item><title>Drop me</title><link>https://x/d</link>'
            b'<category>Finance</category></item>'
            b'</channel></rss>'
        )
        parsed = feedparser.parse(raw)
        since = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        out = parse_entries(
            parsed, "fortune", cap=10, category_filter=["AI"], since=since
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["titleEn"], "Keep me")

    def test_skips_entries_older_than_since(self):
        import feedparser

        raw = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<rss version="2.0"><channel><title>F</title>'
            b'<item><title>Old</title><link>https://x/o</link>'
            b'<pubDate>Wed, 02 Oct 2002 00:00:00 +0000</pubDate></item>'
            b'<item><title>New</title><link>https://x/n</link>'
            b'<pubDate>Wed, 02 Oct 2030 00:00:00 +0000</pubDate></item>'
            b'</channel></rss>'
        )
        parsed = feedparser.parse(raw)
        since = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        out = parse_entries(parsed, "x", cap=10, category_filter=None, since=since)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["titleEn"], "New")

    def test_caps_results(self):
        import feedparser

        items = "".join(
            f"<item><title>T{i}</title><link>https://x/{i}</link></item>" for i in range(20)
        )
        raw = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<rss version="2.0"><channel><title>F</title>' + items.encode() + b'</channel></rss>'
        )
        parsed = feedparser.parse(raw)
        since = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        out = parse_entries(parsed, "x", cap=7, category_filter=None, since=since)
        self.assertEqual(len(out), 7)


class HelpersTests(unittest.TestCase):
    def test_entry_id_is_deterministic_per_source(self):
        import feedparser

        raw = (
            b'<rss version="2.0"><channel><item>'
            b'<title>X</title><link>https://a/x</link></item></channel></rss>'
        )
        entry = feedparser.parse(raw).entries[0]
        self.assertEqual(_entry_id(entry, "src"), "src:https://a/x")

    def test_entry_categories_handles_tags_and_str(self):
        import feedparser

        raw1 = (
            b'<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
            b'<item><title>X</title><link>https://a/x</link>'
            b'<category>AI</category><category>Tech</category></item>'
            b'</channel></rss>'
        )
        raw2 = (
            b'<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
            b'<item><title>Y</title><link>https://a/y</link></item>'
            b'</channel></rss>'
        )
        e1 = feedparser.parse(raw1).entries[0]
        e2 = feedparser.parse(raw2).entries[0]
        self.assertEqual(_entry_categories(e1), ["AI", "Tech"])
        self.assertEqual(_entry_categories(e2), [])

    def test_entry_datetime_parses_pub_date(self):
        import feedparser

        raw = (
            b'<rss version="2.0"><channel><item>'
            b'<title>X</title><link>https://a/x</link>'
            b'<pubDate>Wed, 02 Oct 2030 13:45:00 +0000</pubDate></item>'
            b'</channel></rss>'
        )
        e = feedparser.parse(raw).entries[0]
        parsed = _entry_datetime(e)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2030)
        self.assertEqual(parsed.hour, 13)


class BuildFeedTests(unittest.TestCase):
    def test_reuses_cache_and_translates_only_new(self):
        items = [
            {
                "id": "1",
                "titleEn": "Cached",
                "summaryEn": None,
                "link": "https://x/1",
                "published": None,
                "categories": [],
                "source": "S",
                "sourceId": "s",
                "topic": "MACRO",
            },
            {
                "id": "2",
                "titleEn": "New",
                "summaryEn": None,
                "link": "https://x/2",
                "published": None,
                "categories": [],
                "source": "S",
                "sourceId": "s",
                "topic": "AI",
            },
        ]
        existing = {
            "items": [
                {
                    "id": "1",
                    "titleEn": "Cached",
                    "titleRu": "Из кеша",
                    "translationStatus": "translated",
                }
            ]
        }
        called: list[str] = []

        def tr(t):
            called.append(t)
            return "Новый"

        feed = build_feed(items, existing_feed=existing, translate_fn=tr, now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(called, ["New"])
        titles_ru = {it["id"]: it["titleRu"] for it in feed["items"]}
        self.assertEqual(titles_ru["1"], "Из кеша")
        self.assertEqual(titles_ru["2"], "Новый")
        self.assertEqual(feed["count"], 2)

    def test_failed_translation_keeps_original(self):
        items = [
            {
                "id": "7",
                "titleEn": "Keep me",
                "summaryEn": None,
                "link": "https://x/7",
                "published": None,
                "categories": [],
                "source": "S",
                "sourceId": "s",
                "topic": "AI",
            }
        ]

        def fail(_t):
            raise RuntimeError("nope")

        feed = build_feed(items, existing_feed={}, translate_fn=fail, now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(feed["items"][0]["titleRu"], "Keep me")
        self.assertEqual(feed["items"][0]["translationStatus"], "failed")


class CacheTests(unittest.TestCase):
    def test_cache_ignores_failed_and_original(self):
        feed = {
            "items": [
                {"titleEn": "a", "titleRu": "A", "translationStatus": "translated"},
                {"titleEn": "b", "titleRu": "B", "translationStatus": "failed"},
                {"titleEn": "c", "titleRu": "C", "translationStatus": "original"},
            ]
        }
        cache = _translation_cache(feed)
        self.assertIn("a", cache)
        self.assertNotIn("b", cache)
        self.assertNotIn("c", cache)


class AtomicWriteTests(unittest.TestCase):
    def test_writes_valid_json_no_temp_leftovers(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data" / "news.json"
            save_json_atomic(target, {"count": 1, "items": [{"id": 1}]})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["count"], 1)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])


class SourcesYamlTests(unittest.TestCase):
    def test_loads_real_config(self):
        data = load_sources(Path("scripts/sources.yaml"))
        self.assertGreaterEqual(len(data["sources"]), 6)
        for src in data["sources"]:
            self.assertIn("feed", src)
            self.assertIn("name", src)
            self.assertIn("id", src)


if __name__ == "__main__":
    unittest.main()
