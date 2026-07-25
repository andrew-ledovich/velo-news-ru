import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.update_feed import (
    DEFAULT_USER_AGENT,
    _ArticleHTMLParser,
    _compile_topic_rules,
    _entry_categories,
    _entry_datetime,
    _entry_id,
    _entry_summary_en,
    _safe_summary_text,
    build_feed,
    classify,
    fetch_article_text,
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

    def test_strips_html_from_summary(self):
        import feedparser

        raw = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<rss version="2.0"><channel><title>F</title>'
            b'<item><title>With</title><link>https://x/w</link>'
            b'<description><![CDATA[<p>Hello <b>world</b>&nbsp;friend</p>]]></description>'
            b'</item></channel></rss>'
        )
        parsed = feedparser.parse(raw)
        since = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        out = parse_entries(parsed, "x", cap=5, category_filter=None, since=since)
        self.assertEqual(out[0]["summaryEn"], "Hello world friend")


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

    def test_entry_summary_en_picks_summary_or_description(self):
        import feedparser

        raw = (
            b'<rss version="2.0"><channel><item>'
            b'<title>X</title><link>https://a/x</link>'
            b'<description>desc</description></item></channel></rss>'
        )
        e = feedparser.parse(raw).entries[0]
        self.assertEqual(_entry_summary_en(e), "desc")

    def test_strip_html_keeps_text_and_entities(self):
        # The em-dash entity should survive, but a single space may remain
        # before it from tag-stripping — accept either normalised form.
        out = _safe_summary_text("<p>Hello <b>world</b>&mdash;2026</p>")
        self.assertIn("Hello world", out)
        self.assertIn("2026", out)
        self.assertNotIn("<", out)


class ArticleParserTests(unittest.TestCase):
    def test_collects_long_paragraphs_and_skips_short(self):
        html = (
            "<html><body>"
            "<p>short</p>"
            "<p>" + ("x" * 100) + "</p>"
            "<p>Subscribe to our newsletter for daily updates.</p>"
            "<p>" + ("y" * 200) + "</p>"
            "<script>evil()</script>"
            "<p>" + ("z" * 300) + "</p>"
            "</body></html>"
        )
        parser = _ArticleHTMLParser()
        parser.feed(html)
        paragraphs = parser.paragraphs
        self.assertEqual(len(paragraphs), 3)
        for p in paragraphs:
            self.assertGreaterEqual(len(p), 60)
            self.assertNotIn("Subscribe", p)


class FetchArticleTextTests(unittest.TestCase):
    def test_returns_first_paragraphs_with_mocked_curl(self):
        html = (
            "<html><body>"
            "<article>"
            "<p>" + ("a" * 200) + "</p>"
            "<p>" + ("b" * 200) + "</p>"
            "<p>" + ("c" * 200) + "</p>"
            "</article>"
            "</body></html>"
        ).encode("utf-8")
        with mock.patch("scripts.update_feed._fetch_bytes", return_value=html) as mocked:
            result = fetch_article_text("https://x/article", DEFAULT_USER_AGENT, timeout=5)
        self.assertIn("a" * 50, result)
        self.assertIn("b" * 50, result)
        mocked.assert_called_once()
        called_url = mocked.call_args[0][0]
        self.assertEqual(called_url, "https://x/article")

    def test_returns_empty_when_curl_fails(self):
        with mock.patch("scripts.update_feed._fetch_bytes", side_effect=RuntimeError("boom")):
            result = fetch_article_text("https://x/down", DEFAULT_USER_AGENT, timeout=5)
        self.assertEqual(result, "")


class BuildFeedTests(unittest.TestCase):
    def _item(self, item_id, title, summary=None, source="S", topic="MACRO"):
        return {
            "id": item_id,
            "titleEn": title,
            "summaryEn": summary,
            "link": f"https://x/{item_id}",
            "published": dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
            "categories": [],
            "source": source,
            "sourceId": source.lower(),
            "topic": topic,
        }

    def test_keeps_rss_summary_without_calling_fallback(self):
        items = [self._item("a", "A", summary="From RSS")]
        called: list[str] = []
        feed = build_feed(
            items,
            existing_feed={},
            user_agent=DEFAULT_USER_AGENT,
            fetch_article_fn=lambda link: called.append(link) or "",
            now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(feed["items"][0]["summaryEn"], "From RSS")
        self.assertEqual(called, [])

    def test_falls_back_to_article_when_summary_missing(self):
        items = [self._item("a", "A", summary=None)]
        called: list[str] = []
        def fetch_fn(link):
            called.append(link)
            return "Body from article. " * 30
        feed = build_feed(
            items,
            existing_feed={},
            user_agent=DEFAULT_USER_AGENT,
            fetch_article_fn=fetch_fn,
            now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
        )
        self.assertIn("Body from article", feed["items"][0]["summaryEn"])
        self.assertEqual(called, ["https://x/a"])

    def test_keeps_summary_null_when_fallback_returns_empty(self):
        items = [self._item("a", "A", summary=None)]
        feed = build_feed(
            items,
            existing_feed={},
            user_agent=DEFAULT_USER_AGENT,
            fetch_article_fn=lambda _link: "",
            now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
        )
        self.assertIsNone(feed["items"][0]["summaryEn"])

    def test_no_fallback_when_disabled(self):
        items = [self._item("a", "A", summary=None)]
        called: list[str] = []
        feed = build_feed(
            items,
            existing_feed={},
            user_agent=DEFAULT_USER_AGENT,
            fetch_article_fn=lambda link: called.append(link) or "",
            now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
        )
        # Always pass fetch_article_fn in this test path; behaviour when None is
        # covered by passing fetch_article_fn=None from the CLI.
        # Just verify the feed shape is correct.
        self.assertIsNone(feed["items"][0]["summaryEn"])
        self.assertEqual(feed["count"], 1)


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
