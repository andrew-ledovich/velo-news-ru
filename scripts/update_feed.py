#!/usr/bin/env python3
"""Fetch RSS feeds, classify by topic, fetch article summaries, write static feed.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import feedparser
import yaml

GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
DEFAULT_USER_AGENT = "velo-news-ru/3.0 (+https://github.com/andrew-ledovich/velo-news-ru)"


# ---------- text helpers ----------


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value), flags=re.DOTALL)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_html(value: Any) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value), flags=re.DOTALL)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&mdash;", "—")
        .replace("&ndash;", "–")
        .replace("&hellip;", "…")
    )
    return re.sub(r"\s+", " ", text).strip()


# ---------- sources config ----------


def load_sources(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"sources yaml root must be a mapping: {path}")
    return data


# ---------- classification ----------


def _compile_topic_rules(rules_by_topic: dict[str, list[str]]) -> list[tuple[str, list[re.Pattern[str]]]]:
    compiled: list[tuple[str, list[re.Pattern[str]]]] = []
    for topic, patterns in rules_by_topic.items():
        compiled.append(
            (topic, [re.compile(p, re.IGNORECASE) for p in patterns])
        )
    return compiled


def classify(
    title: str,
    summary: str,
    categories: list[str],
    compiled_rules: list[tuple[str, list[re.Pattern[str]]]],
    topic_boost: list[str] | None = None,
) -> str:
    haystack_parts = [title or "", summary or ""] + (categories or [])
    haystack = _strip_html(" \n ".join(haystack_parts))

    for topic, patterns in compiled_rules:
        if topic == "macro":
            continue
        for pattern in patterns:
            if pattern.search(haystack):
                return topic.upper()
    return "MACRO"


# ---------- RSS fetch ----------


def _fetch_bytes(url: str, user_agent: str, timeout: int, extra_headers: list[str] | None = None) -> bytes:
    cmd = [
        "curl",
        "--compressed",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--max-time",
        str(timeout),
        "--user-agent",
        user_agent,
    ]
    for h in extra_headers or []:
        cmd += ["-H", h]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed (exit {result.returncode}) for {url}")
    return result.stdout


def fetch_feed(url: str, user_agent: str, timeout: int) -> feedparser.FeedParserDict:
    raw = _fetch_bytes(
        url,
        user_agent,
        timeout,
        extra_headers=[
            "Accept: application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            "Accept-Language: en-US,en;q=0.9",
        ],
    )
    return feedparser.parse(raw)


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = getattr(entry, key, None)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    raw = entry.get("published") or entry.get("updated") or entry.get("created")
    if raw:
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
    return None


def _entry_categories(entry: Any) -> list[str]:
    out: list[str] = []
    for tag in entry.get("tags") or []:
        if isinstance(tag, dict):
            term = tag.get("term")
            if term:
                out.append(str(term))
    if not out and entry.get("category"):
        out.append(str(entry["category"]))
    return out


def _entry_id(entry: Any, source_id: str) -> str:
    guid = entry.get("id") or entry.get("link") or entry.get("title")
    if guid:
        return f"{source_id}:{guid}"
    digest = hashlib.sha1(
        (str(entry.get("title")) + str(entry.get("link"))).encode("utf-8", "ignore")
    ).hexdigest()[:16]
    return f"{source_id}:h{digest}"


# ---------- HTML aggregate (stockanalysis.com) ----------


def _parse_stockanalysis(html: str) -> list[dict[str, Any]]:
    """Extract (title, link) from stockanalysis.com/news/all-stocks/ HTML."""
    # Match: <a href="https://.../news/...">...TITLE TEXT...</a>
    # The link may have many attributes; the visible text may span newlines and
    # contain inline tags (e.g. <span>). Be permissive: anything between the
    # opening <a ...> and the FIRST closing </a> belongs to the title.
    anchor_pattern = re.compile(
        r'<a\b([^>]*?)href="(https?://[^"]+/news/[^"]+)"([^>]*)>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in anchor_pattern.finditer(html):
        link = match.group(2).strip()
        if not link or link in seen:
            continue
        # Skip nav links back to the index/press pages.
        if "stockanalysis.com/news/all-stocks" in link:
            continue
        if "stockanalysis.com/news/press-releases" in link:
            continue
        inner = match.group(4)
        # Drop nested anchors' nested text? strip recursively any <…> then condense.
        title = re.sub(r"<[^>]+>", " ", inner)
        title = _clean(title)
        if len(title) < 30:
            continue
        seen.add(link)
        out.append(
            {
                "id": f"stockanalysis:{hashlib.sha1(link.encode('utf-8')).hexdigest()[:16]}",
                "titleEn": title,
                "link": link,
            }
        )
    return out


def fetch_html_aggregate(url: str, user_agent: str, timeout: int) -> list[dict[str, Any]]:
    raw = _fetch_bytes(
        url,
        user_agent,
        timeout,
        extra_headers=[
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language: en-US,en;q=0.9",
        ],
    )
    html = raw.decode("utf-8", "ignore")
    host_match = re.search(r"https?://(?:www\.)?([^/]+)/", url)
    host = host_match.group(1) if host_match else ""
    if "stockanalysis.com" in host:
        return _parse_stockanalysis(html)
    return []


# Boilerplate filter: paragraphs matching any of these substrings (case-insensitive)
# are dropped before the summary is built. Covers the common paywall / nav / byline
# patterns seen across Kitco, PYMNTS, Invezz, BusinessWire and friends.
_BOILERPLATE_PATTERNS = [
    r"subscribe to",
    r"sign up for",
    r"create an account",
    r"already a subscriber",
    r"log in",
    r"unlock this article",
    r"complete the form",
    r"enjoy unlimited free",
    r"to read the full story",
    r"continue reading",
    r"read more",
    r"©\s*\d{4}",
    r"all rights reserved",
    r"this website is using a security service",
    r"please enable cookies",
    r"please enable javascript",
    r"accept (all )?cookies",
    r"privacy policy",
    r"terms of (service|use)",
    r"manage cookies",
    r"do not (sell|share) my",
    r"advertisement",
    r"newsletter",
    r"diverse team of journalists",
    r"accuracy and objectivity",
    r"related articles",
    r"related stories",
    r"you may also like",
    r"share this",
    r"follow us on",
    r"tags?:",
    r"filed under",
    r"categories?:",
    r"trending",
    r"most read",
    r"most popular",
    r"copyright",
    r"kitco news has",
    r"our goal is to help people make",
    r"by entering your email",
    r"submit",
    r"contact us",
    r"about us",
    r"press releases?",
    r"all press releases",
    r"view all",
    r"show more",
    r"show less",
]
_BOILERPLATE_RE = re.compile("|".join(_BOILERPLATE_PATTERNS), re.IGNORECASE)


def _extract_paragraphs_from_html(
    html: str,
    min_chars: int = 80,
    max_chars: int = 1500,
    max_paragraphs: int = 3,
) -> list[str]:
    """Pull article-body <p> blocks from raw HTML, filtering boilerplate."""
    # Prefer <article> if present, else fall back to full body.
    m = re.search(r"<article[^>]*>(.*?)</article>", html, flags=re.DOTALL | re.IGNORECASE)
    scope = m.group(1) if m else html
    # Match <p>...</p> with non-trivial text. Use DOTALL so we capture
    # paragraphs that contain inline tags.
    paragraph_re = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
    out: list[str] = []
    total = 0
    for match in paragraph_re.finditer(scope):
        inner = match.group(1)
        text = re.sub(r"<[^>]+>", " ", inner)
        text = _strip_html(text)
        if not text:
            continue
        if len(text) < min_chars:
            continue
        if _BOILERPLATE_RE.search(text):
            continue
        out.append(text)
        total += len(text)
        if len(out) >= max_paragraphs or total >= max_chars:
            break
    return out


def fetch_article_summary(
    url: str,
    user_agent: str,
    timeout: int = 15,
    min_chars: int = 80,
    max_chars: int = 1500,
    max_paragraphs: int = 3,
) -> str:
    """Fetch one article page and return a plain-text summary, or '' on failure."""
    try:
        html_bytes = _fetch_bytes(
            url,
            user_agent,
            timeout,
            extra_headers=[
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language: en-US,en;q=0.9",
            ],
        )
    except Exception:
        return ""
    html = html_bytes.decode("utf-8", "ignore")
    paragraphs = _extract_paragraphs_from_html(
        html,
        min_chars=min_chars,
        max_chars=max_chars,
        max_paragraphs=max_paragraphs,
    )
    if not paragraphs:
        return ""
    return " ".join(paragraphs).strip()


def _entry_summary_en(entry: Any) -> str:
    raw = entry.get("summary") or entry.get("description") or entry.get("subtitle")
    cleaned = _strip_html(raw)
    return cleaned


class _ArticleHTMLParser(HTMLParser):
    """Pick the largest <p> block inside the article body. Naive but good enough as fallback."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._depth_in_p = 0
        self._current: list[str] = []
        self._skip_depth = 0
        self._skip_tags = {"script", "style", "noscript", "svg", "form", "header", "footer", "nav", "aside", "iframe"}

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._skip_depth += 1
            return
        if tag == "p":
            self._depth_in_p += 1
            self._current.append("")

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "p" and self._depth_in_p > 0:
            self._depth_in_p -= 1
            text = " ".join("".join(self._current).split()).strip()
            if text and len(text) >= 60 and "cookie" not in text.lower() and "subscribe" not in text.lower():
                self._chunks.append(text)
            self._current = []

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._depth_in_p:
            self._current.append(data)

    @property
    def paragraphs(self) -> list[str]:
        return self._chunks


def fetch_article_text(
    url: str, user_agent: str, timeout: int = 15, max_paragraphs: int = 4, max_chars: int = 1200
) -> str:
    """Fetch the article HTML and pull the first paragraphs. Conservative."""
    try:
        html_bytes = _fetch_bytes(
            url,
            user_agent,
            timeout,
            extra_headers=[
                "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language: en-US,en;q=0.9",
            ],
        )
    except Exception:
        return ""
    html = html_bytes.decode("utf-8", "ignore")
    # Optional: scope to <article> if present, to skip nav/footer paragraphs.
    m = re.search(r"<article[^>]*>(.*?)</article>", html, flags=re.DOTALL | re.IGNORECASE)
    if m:
        html = m.group(1)
    parser = _ArticleHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        return ""
    chunks: list[str] = []
    total = 0
    for p in parser.paragraphs:
        chunks.append(p)
        total += len(p)
        if len(chunks) >= max_paragraphs or total >= max_chars:
            break
    return "\n\n".join(chunks)


def parse_entries(
    parsed: feedparser.FeedParserDict,
    source_id: str,
    cap: int,
    category_filter: list[str] | None,
    since: datetime,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in parsed.entries:
        title = _clean(entry.get("title"))
        link = _clean(entry.get("link"))
        summary = _entry_summary_en(entry)
        published = _entry_datetime(entry)
        if not title or not link:
            continue
        if published and published < since:
            continue
        categories = _entry_categories(entry)
        if category_filter:
            wanted = {c.strip().lower() for c in category_filter}
            if not any(cat.lower() in wanted for cat in categories):
                continue
        out.append(
            {
                "id": _entry_id(entry, source_id),
                "titleEn": title,
                "summaryEn": summary[:1500] if summary else None,
                "link": link,
                "published": published,
                "categories": categories,
            }
        )
    out.sort(key=lambda x: x["published"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return out[:cap]


# ---------- translation (optional) ----------


def _parse_google_translation(data: list[Any], fallback: str) -> str:
    if not data or not isinstance(data[0], list):
        raise ValueError("translation missing")
    parts = []
    for segment in data[0]:
        if isinstance(segment, list) and segment:
            part = _clean(segment[0])
            if part:
                parts.append(part)
    out = " ".join(parts).strip()
    if not out:
        raise ValueError("translation empty")
    return out or fallback


def _google_translate_array(text: str, user_agent: str, timeout: int) -> list[Any]:
    query = urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text[:4500]}
    )
    raw = _fetch_bytes(f"{GOOGLE_TRANSLATE_URL}?{query}", user_agent, timeout)
    data = json.loads(raw.decode("utf-8", "ignore"))
    if not isinstance(data, list):
        raise ValueError("unexpected google translate response")
    return data


def translate_to_russian(text: str, user_agent: str, timeout: int = 20) -> str:
    return _parse_google_translation(_google_translate_array(text, user_agent, timeout), text)


# ---------- feed building ----------


def _safe_summary_text(text: str) -> str:
    return _strip_html(text)


def build_feed(
    items: list[dict[str, Any]],
    existing_feed: dict[str, Any] | None,
    user_agent: str,
    fetch_article_fn: Callable[[str], str] | None = None,
    delay_seconds: float = 0.0,
    max_summary_chars: int = 1200,
    now: datetime | None = None,
) -> dict[str, Any]:
    seen: set[str] = set()
    out_items: list[dict[str, Any]] = []

    for item in items:
        item_id = item["id"]
        if item_id in seen:
            continue
        seen.add(item_id)

        title_en = _clean(item.get("titleEn"))
        summary_en = _safe_summary_text(item.get("summaryEn") or "")
        if not summary_en and fetch_article_fn and item.get("link"):
            fetched = _strip_html(fetch_article_fn(item["link"]))
            if fetched:
                summary_en = fetched[:max_summary_chars]
            if delay_seconds:
                time.sleep(delay_seconds)

        published = item.get("published")
        out_items.append(
            {
                "id": item_id,
                "titleEn": title_en,
                "summaryEn": summary_en or None,
                "source": item.get("source"),
                "topic": item.get("topic"),
                "link": item.get("link"),
                "published": published.isoformat().replace("+00:00", "Z") if published else None,
            }
        )

    now = now or datetime.now(timezone.utc)
    return {
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "sourceCount": len({i.get("source") for i in out_items if i.get("source")}),
        "count": len(out_items),
        "items": out_items,
    }


# ---------- IO ----------


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# ---------- pipeline ----------


def collect(
    sources: dict[str, Any],
    user_agent: str,
    request_timeout: int,
    since: datetime,
    workers: int = 6,
) -> list[dict[str, Any]]:
    compiled = _compile_topic_rules(sources.get("classification", {}))
    boosts: dict[str, list[str]] = {}
    for src in sources["sources"]:
        if not src.get("enabled", True):
            continue
        boosts[src["id"]] = src.get("topic_boost") or []

    def _one(src: dict[str, Any]) -> list[dict[str, Any]]:
        feed_type = src.get("feed_type", "rss")
        try:
            if feed_type == "html_aggregate":
                items = fetch_html_aggregate(
                    src["feed"], user_agent, request_timeout
                )
                # Parallel summary fetch.
                workers = int(src.get("summary_workers", 4))
                max_paras = int(src.get("summary_max_paragraphs", 3))
                min_chars = int(src.get("summary_min_chars", 80))
                max_chars = int(src.get("summary_max_chars", 1500))
                if items and workers > 0:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        future_to_raw = {
                            pool.submit(
                                fetch_article_summary,
                                raw["link"],
                                user_agent,
                                request_timeout,
                                min_chars,
                                max_chars,
                                max_paras,
                            ): raw
                            for raw in items
                        }
                        for fut in as_completed(future_to_raw):
                            raw = future_to_raw[fut]
                            raw["summaryEn"] = fut.result() or None
                else:
                    for raw in items:
                        raw["summaryEn"] = None
                entries: list[dict[str, Any]] = []
                for raw in items[: int(src.get("cap", 12))]:
                    entries.append(
                        {
                            "id": raw["id"],
                            "titleEn": raw["titleEn"],
                            "summaryEn": raw.get("summaryEn"),
                            "link": raw["link"],
                            "published": None,
                            "categories": [],
                        }
                    )
            else:
                parsed = fetch_feed(src["feed"], user_agent, request_timeout)
                entries = parse_entries(
                    parsed,
                    source_id=src["id"],
                    cap=int(src.get("cap", 12)),
                    category_filter=src.get("category_filter"),
                    since=since,
                )
        except Exception as exc:
            print(f"warning: feed {src['id']} failed: {exc}", file=sys.stderr, flush=True)
            return []
        out: list[dict[str, Any]] = []
        for e in entries:
            topic = classify(
                e["titleEn"],
                e.get("summaryEn") or "",
                e.get("categories") or [],
                compiled,
                topic_boost=boosts.get(src["id"]),
            )
            out.append(
                {
                    "id": e["id"],
                    "titleEn": e["titleEn"],
                    "summaryEn": e.get("summaryEn"),
                    "link": e["link"],
                    "published": e.get("published"),
                    "categories": e.get("categories") or [],
                    "source": src["name"],
                    "sourceId": src["id"],
                    "topic": topic,
                }
            )
        return out

    all_items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, src): src for src in sources["sources"] if src.get("enabled", True)}
        for fut in as_completed(futures):
            all_items.extend(fut.result())
    return all_items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default="scripts/sources.yaml")
    parser.add_argument("--output", default="docs/data/news.json")
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--keep-days", type=int, default=0)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--no-fallback", action="store_true", help="Skip HTML article fetch when RSS summary is empty")
    args = parser.parse_args()

    sources = load_sources(Path(args.sources))
    settings = sources.get("settings", {})
    user_agent = settings.get("user_agent", DEFAULT_USER_AGENT)
    request_timeout = int(settings.get("request_timeout", 25))
    max_items = int(args.max_items or settings.get("max_items", 200))
    keep_days = int(args.keep_days or settings.get("keep_days", 7))

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=keep_days)

    raw_items = collect(sources, user_agent, request_timeout, since, workers=args.workers)
    if not raw_items:
        raise RuntimeError("no items from any source; refusing to overwrite the feed")

    raw_items.sort(key=lambda x: x.get("published") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    raw_items = raw_items[:max_items]

    output = Path(args.output)
    fetch_article_fn: Callable[[str], str] | None
    if args.no_fallback:
        fetch_article_fn = None
    else:
        def _fetch(link: str) -> str:
            return fetch_article_text(link, user_agent=user_agent, timeout=20)

        fetch_article_fn = _fetch

    feed = build_feed(
        raw_items,
        existing_feed=load_json(output),
        user_agent=user_agent,
        fetch_article_fn=fetch_article_fn,
        delay_seconds=0.0,
        now=now,
    )
    save_json_atomic(output, feed)

    have_summary = sum(1 for i in feed["items"] if i.get("summaryEn"))
    print(
        f"saved {feed['count']} items to {output} from {feed['sourceCount']} sources, "
        f"with summary: {have_summary}/{feed['count']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
