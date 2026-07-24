#!/usr/bin/env python3
"""Fetch Velo news, translate new headlines to Russian, and export static JSON."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

VELO_NEWS_URL = "https://velo.xyz/api/n/news"
GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
USER_AGENT = "velo-news-ru/1.0 (+https://github.com/andrew-ledovich/velo-news-ru)"


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number_or_none(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        parsed = float(value)
        return int(parsed) if parsed.is_integer() else parsed
    except (TypeError, ValueError):
        return None


def normalize_stories(payload: dict[str, Any], limit: int = 250) -> list[dict[str, Any]]:
    """Validate and normalize the public Velo response."""
    raw_stories = payload.get("stories")
    if not isinstance(raw_stories, list):
        raise ValueError("Velo payload does not contain a stories list")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for raw in raw_stories:
        if not isinstance(raw, dict):
            continue
        title = _text(raw.get("headline"))
        story_id = _text(raw.get("id"))
        timestamp = _integer(raw.get("effectiveTime") or raw.get("time"))
        if not title or not story_id or timestamp <= 0 or story_id in seen_ids:
            continue

        seen_ids.add(story_id)
        coins = []
        seen_coins = set()
        for coin in raw.get("coins") or []:
            clean_coin = _text(coin).upper()
            if clean_coin and clean_coin not in seen_coins:
                seen_coins.add(clean_coin)
                coins.append(clean_coin)

        link = _text(raw.get("link")) or None
        if link and not link.startswith(("https://", "http://")):
            link = None

        normalized.append(
            {
                "id": int(story_id) if story_id.isdigit() else story_id,
                "titleEn": title,
                "source": _text(raw.get("source")) or "Velo",
                "priority": max(1, min(3, _integer(raw.get("priority"), 2))),
                "summary": _text(raw.get("summary")) or None,
                "link": link,
                "time": timestamp,
                "effectivePrice": _number_or_none(raw.get("effectivePrice")),
                "coins": coins,
            }
        )

    normalized.sort(key=lambda story: story["time"], reverse=True)
    return normalized[:limit]


def _curl_json(url: str, timeout: int = 25) -> dict[str, Any]:
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
            USER_AGENT,
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload


def fetch_velo_news(url: str = VELO_NEWS_URL) -> dict[str, Any]:
    return _curl_json(url)


def translate_to_russian(text: str, timeout: int = 20) -> str:
    """Translate one short headline through Google's public web endpoint."""
    query = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": "auto",
            "tl": "ru",
            "dt": "t",
            "q": text[:4500],
        }
    )
    url = f"{GOOGLE_TRANSLATE_URL}?{query}"

    try:
        data = _curl_json(url, timeout=timeout)
    except (ValueError, json.JSONDecodeError):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)

    # Google returns an array; the curl helper enforces dicts, so parse directly.
    if isinstance(data, dict):
        raise ValueError("Unexpected Google Translate response")
    return _parse_google_translation(data, text)


def _fetch_translation_array(text: str, timeout: int = 20) -> list[Any]:
    query = urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text[:4500]}
    )
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
            USER_AGENT,
            f"{GOOGLE_TRANSLATE_URL}?{query}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise ValueError("Unexpected Google Translate response")
    return data


def _parse_google_translation(data: list[Any], fallback: str) -> str:
    if not data or not isinstance(data[0], list):
        raise ValueError("Translation is missing")
    parts = []
    for segment in data[0]:
        if isinstance(segment, list) and segment:
            part = _text(segment[0])
            if part:
                parts.append(part)
    translated = " ".join(parts).strip()
    if not translated:
        raise ValueError("Translation is empty")
    return translated or fallback


def translate_headline(text: str) -> str:
    return _parse_google_translation(_fetch_translation_array(text), text)


def _translation_cache(existing_feed: dict[str, Any] | None) -> dict[str, tuple[str, str]]:
    cache: dict[str, tuple[str, str]] = {}
    if not isinstance(existing_feed, dict):
        return cache
    for item in existing_feed.get("items") or []:
        if not isinstance(item, dict):
            continue
        title_en = _text(item.get("titleEn"))
        title_ru = _text(item.get("titleRu"))
        status = _text(item.get("translationStatus")) or "translated"
        if title_en and title_ru and status == "translated":
            cache[title_en] = (title_ru, status)
    return cache


def build_feed(
    stories: list[dict[str, Any]],
    existing_feed: dict[str, Any] | None,
    translate_fn: Callable[[str], str] = translate_headline,
    generated_at: str | None = None,
    delay_seconds: float = 0.12,
) -> dict[str, Any]:
    cache = _translation_cache(existing_feed)
    items = []

    for story in stories:
        item = dict(story)
        title_en = _text(item.get("titleEn"))
        cached = cache.get(title_en)
        if cached:
            title_ru, status = cached
        else:
            try:
                title_ru = _text(translate_fn(title_en))
                if not title_ru:
                    raise ValueError("translator returned an empty title")
                status = "translated"
            except Exception as exc:  # Keep the feed available on translator outages.
                print(f"warning: translation failed for {title_en!r}: {exc}", flush=True)
                title_ru = title_en
                status = "failed"
            if delay_seconds:
                time.sleep(delay_seconds)

        item["titleRu"] = title_ru
        item["translationStatus"] = status
        items.append(item)

    timestamp = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "generatedAt": timestamp,
        "sourceUrl": "https://velo.xyz/news",
        "apiUrl": VELO_NEWS_URL,
        "count": len(items),
        "items": items,
    }


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/data/news.json")
    parser.add_argument("--input", help="Read a saved Velo response instead of the network")
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument("--no-translate", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    payload = load_json(Path(args.input)) if args.input else fetch_velo_news()
    stories = normalize_stories(payload, limit=max(1, args.limit))
    if not stories:
        raise RuntimeError("Velo returned no usable stories; refusing to overwrite the feed")

    existing = load_json(output)
    translator = (lambda text: text) if args.no_translate else translate_headline
    feed = build_feed(stories, existing_feed=existing, translate_fn=translator)
    if args.no_translate:
        for item in feed["items"]:
            item["translationStatus"] = "original"
    save_json_atomic(output, feed)
    print(f"saved {feed['count']} stories to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
