#!/usr/bin/env python3
"""Fetch RSS feeds, classify by topic, translate to Russian, write static feed.json."""

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
from pathlib import Path
from typing import Any, Callable

import feedparser
import yaml

GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
DEFAULT_USER_AGENT = "velo-news-ru/2.0 (+https://github.com/andrew-ledovich/velo-news-ru)"


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
    haystack = " \n ".join(haystack_parts)
    haystack = _strip_html(haystack)

    for topic, patterns in compiled_rules:
        # macro is the catch-all (".*") and is checked last
        if topic == "macro":
            continue
        for pattern in patterns:
            if pattern.search(haystack):
                return topic.upper()
    return "MACRO"


# ---------- RSS fetch ----------


def _fetch_bytes(url: str, user_agent: str, timeout: int) -> bytes:
    result = subprocess.run(
        [
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
            url,
        ],
        check=True,
        capture_output=True,
    )
    return result.stdout


def fetch_feed(url: str, user_agent: str, timeout: int) -> feedparser.FeedParserDict:
    raw = _fetch_bytes(url, user_agent, timeout)
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
        summary = _strip_html(entry.get("summary") or entry.get("description"))
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
                "summaryEn": summary[:1200] if summary else None,
                "link": link,
                "published": published,
                "categories": categories,
            }
        )
    out.sort(key=lambda x: x["published"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return out[:cap]


# ---------- translation ----------


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


def _translation_cache(feed: dict[str, Any] | None) -> dict[str, tuple[str, str]]:
    cache: dict[str, tuple[str, str]] = {}
    if not isinstance(feed, dict):
        return cache
    for item in feed.get("items") or []:
        if not isinstance(item, dict):
            continue
        text_en = _clean(item.get("titleEn"))
        text_ru = _clean(item.get("titleRu"))
        status = _clean(item.get("translationStatus")) or "translated"
        if text_en and text_ru and status == "translated":
            cache[text_en] = (text_ru, status)
    return cache


def build_feed(
    items: list[dict[str, Any]],
    existing_feed: dict[str, Any] | None,
    translate_fn: Callable[[str], str],
    delay_seconds: float = 0.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    cache = _translation_cache(existing_feed)
    out_items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in items:
        item_id = item["id"]
        if item_id in seen:
            continue
        seen.add(item_id)
        title_en = _clean(item.get("titleEn"))
        title_ru: str
        status: str
        cached = cache.get(title_en)
        if cached:
            title_ru, status = cached
        else:
            try:
                translated = _clean(translate_fn(title_en))
                if not translated:
                    raise ValueError("translator returned empty")
                title_ru = translated
                status = "translated"
            except Exception as exc:
                print(
                    f"warning: translation failed for {title_en[:60]!r}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                title_ru = title_en
                status = "failed"
            if delay_seconds:
                time.sleep(delay_seconds)

        published = item.get("published")
        out_items.append(
            {
                "id": item_id,
                "titleEn": title_en,
                "titleRu": title_ru,
                "summaryEn": item.get("summaryEn"),
                "source": item.get("source"),
                "topic": item.get("topic"),
                "link": item.get("link"),
                "published": published.isoformat().replace("+00:00", "Z") if published else None,
                "translationStatus": status,
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
        try:
            parsed = fetch_feed(src["feed"], user_agent, request_timeout)
        except Exception as exc:
            print(f"warning: feed {src['id']} failed: {exc}", file=sys.stderr, flush=True)
            return []
        entries = parse_entries(
            parsed,
            source_id=src["id"],
            cap=int(src.get("cap", 12)),
            category_filter=src.get("category_filter"),
            since=since,
        )
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
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--keep-days", type=int, default=0)
    args = parser.parse_args()

    sources = load_sources(Path(args.sources))
    settings = sources.get("settings", {})
    user_agent = settings.get("user_agent", DEFAULT_USER_AGENT)
    request_timeout = int(settings.get("request_timeout", 25))
    delay = 0.0 if args.no_translate else float(settings.get("per_request_delay_seconds", 0.15))
    max_items = int(args.max_items or settings.get("max_items", 200))
    keep_days = int(args.keep_days or settings.get("keep_days", 7))

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=keep_days)

    raw_items = collect(sources, user_agent, request_timeout, since)
    if not raw_items:
        raise RuntimeError("no items from any source; refusing to overwrite the feed")

    raw_items.sort(key=lambda x: x.get("published") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    raw_items = raw_items[:max_items]

    output = Path(args.output)
    existing = load_json(output)

    def _translate(text: str) -> str:
        if args.no_translate:
            return text
        return translate_to_russian(text, user_agent, request_timeout)

    feed = build_feed(raw_items, existing_feed=existing, translate_fn=_translate, delay_seconds=delay, now=now)
    if args.no_translate:
        for item in feed["items"]:
            item["translationStatus"] = "original"
    save_json_atomic(output, feed)
    print(
        f"saved {feed['count']} items to {output} from {feed['sourceCount']} sources",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
