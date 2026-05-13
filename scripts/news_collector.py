"""News Collector Script

This script reads the list of tickers from ``rules.md`` and fetches the latest
news for each ticker using the free ``yfinance`` API (``Ticker.news``).  The
retrieved items are stored in ``data/news/{ticker}_news.json``.

Key features
------------
* **Incremental updates** – Existing news are loaded, new items are merged and
  deduplicated (by ``title`` and ``published`` timestamp).
* **Retention policy** – Each file keeps at most the most recent 50 items **or**
  items not older than 14 days, whichever results in fewer records.
* **Automatic cleanup** – JSON files older than 30 days are removed after each
  run.
* **Initial back‑fill** – On the first execution the script keeps only news
  published within the last 30 days.

The script is deliberately self‑contained so it can be executed independently
of the existing pipeline.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Set

try:
    import yfinance as yf
    from textblob import TextBlob
except ImportError as e:  # pragma: no cover
    sys.stderr.write(f"Required packages missing: {e}. Install with 'pip install yfinance textblob'\n")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[1]  # project root (d:/Program/AI_Investment_Lab)
RULES_FILE = BASE_DIR / "rules.md"
NEWS_DIR = BASE_DIR / "data" / "news"
NEWS_DIR.mkdir(parents=True, exist_ok=True)

MAX_ITEMS = 50
# Retain news for the most recent 30 days (instead of the previous 14‑day window)
MAX_DAYS = 30
INITIAL_BACKFILL_DAYS = 30


def extract_tickers(rules_path: Path) -> Set[str]:
    """Collect ticker symbols from ``rules.md`` **and** ``data/watchlist.csv``.

    * ``rules.md`` – contains a markdown table; we extract the first column
      using a regular expression (uppercase letters, 1‑5 characters).
    * ``data/watchlist.csv`` – a single line of comma‑separated tickers.

    The function returns a deduplicated ``set`` of all tickers found in either
    source, allowing the script to stay in sync with the watchlist without
    requiring manual edits to ``rules.md``.
    """
    tickers: Set[str] = set()

    # --- Extract from rules.md ---
    ticker_pattern = re.compile(r"^\|\s*([A-Z]{1,5})\s*\|", re.MULTILINE)
    if rules_path.is_file():
        content = rules_path.read_text(encoding="utf-8")
        tickers.update({m.group(1) for m in ticker_pattern.finditer(content)})

    # --- Extract from watchlist.csv (comma‑separated) ---
    watchlist_path = BASE_DIR / "data" / "watchlist.csv"
    if watchlist_path.is_file():
        line = watchlist_path.read_text(encoding="utf-8").strip()
        # Split on commas and strip whitespace; ignore empty entries.
        csv_tickers = [t.strip().upper() for t in line.split(",") if t.strip()]
        tickers.update(csv_tickers)

    return tickers


def load_existing_news(file_path: Path) -> List[Dict[str, Any]]:
    if not file_path.is_file():
        return []
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupted file – start fresh
        return []


def deduplicate(news: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicate entries based on ``title`` + ``published``.

    The function preserves the original order (newest first) while discarding
    later duplicates.
    """
    seen: Set[tuple] = set()
    unique: List[Dict[str, Any]] = []
    for item in news:
        key = (item.get("title"), item.get("published"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _parse_iso_datetime(dt_str: str) -> datetime:
    """Parse an ISO‑8601 string that may end with ``Z``.

    ``datetime.fromisoformat`` understands the ``+00:00`` offset but not a plain
    trailing ``Z``.  We strip a trailing ``Z`` and then ensure the resulting
    datetime is timezone‑aware in UTC.
    """
    # Remove trailing Z if present (common in Yahoo Finance timestamps).
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        # Assume UTC when no offset is provided.
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def filter_by_date(news: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    """Return items whose ``published`` timestamp is within the last *days*.

    All timestamps are normalised to UTC aware ``datetime`` objects before the
    comparison to avoid naive/aware mismatches.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filtered: List[Dict[str, Any]] = []
    for n in news:
        try:
            pub_dt = _parse_iso_datetime(n["published"])
        except Exception:
            continue
        if pub_dt >= cutoff:
            filtered.append(n)
    return filtered


def analyze_sentiment(text: str) -> Dict[str, float]:
    """使用TextBlob进行情感分析，返回polarity和subjectivity分数。
    
    polarity: -1(负面) 到 +1(正面)
    subjectivity: 0(客观) 到 1(主观)
    """
    if not text or not text.strip():
        return {"polarity": 0.0, "subjectivity": 0.0}
    
    blob = TextBlob(text)
    return {
        "polarity": round(blob.sentiment.polarity, 3),
        "subjectivity": round(blob.sentiment.subjectivity, 3)
    }


def fetch_news_for_ticker(ticker: str) -> List[Dict[str, Any]]:
    """Fetch news using yfinance and normalise the fields.

    The structure returned by ``yfinance`` (as of v1.3.0) is a list where each
    element contains a top‑level ``content`` dictionary.  Relevant fields are:

    * ``content.title`` – headline
    * ``content.summary`` or ``content.description`` – short summary
    * ``content.provider.displayName`` – source name
    * ``content.pubDate`` – ISO‑8601 timestamp (UTC)
    * ``content.canonicalUrl.url`` – link to the article

    This function extracts those fields and returns a flat list of dictionaries
    with a consistent schema used by the rest of the script.
    """
    yf_ticker = yf.Ticker(ticker)
    raw = yf_ticker.news  # type: ignore[attr-defined]
    result: List[Dict[str, Any]] = []
    for entry in raw:
        content = entry.get("content") or {}
        title = content.get("title")
        if not title:
            continue
        # Prefer the explicit ``summary`` field; fall back to ``description``.
        summary = content.get("summary") or content.get("description") or ""
        publisher = (
            content.get("provider", {}).get("displayName")
            or content.get("provider", {}).get("sourceId")
            or ""
        )
        # ``pubDate`` is already an ISO‑8601 string.
        published = content.get("pubDate") or ""
        # Link may be under ``canonicalUrl`` or ``clickThroughUrl``.
        link = (
            content.get("canonicalUrl", {}).get("url")
            or content.get("clickThroughUrl", {}).get("url")
            or ""
        )
        # 合并标题和摘要进行情感分析
        full_text = f"{title} {summary}"
        sentiment = analyze_sentiment(full_text)
        
        result.append(
            {
                "title": title,
                "published": published,
                "publisher": publisher,
                "summary": summary,
                "link": link,
                "sentiment_polarity": sentiment["polarity"],
                "sentiment_subjectivity": sentiment["subjectivity"],
            }
        )
    return result


def prune_old_files(directory: Path, days: int = 30) -> None:
    """Delete JSON files older than *days* based on their modification time."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    for file in directory.glob("*_news.json"):
        try:
            mtime = datetime.utcfromtimestamp(file.stat().st_mtime)
            if mtime < cutoff:
                file.unlink()
        except Exception:
            continue


def main() -> None:
    tickers = extract_tickers(RULES_FILE)
    if not tickers:
        sys.stderr.write("No tickers found in rules.md\n")
        sys.exit(1)

    for ticker in tickers:
        news_file = NEWS_DIR / f"{ticker}_news.json"
        existing = load_existing_news(news_file)

        fetched = fetch_news_for_ticker(ticker)
        # Keep only news within the initial back‑fill window when the file is new.
        if not existing:
            fetched = filter_by_date(fetched, INITIAL_BACKFILL_DAYS)

        combined = fetched + existing
        combined = sorted(combined, key=lambda x: x["published"], reverse=True)
        combined = deduplicate(combined)
        # Apply retention policy: max 50 items or not older than 14 days.
        recent = filter_by_date(combined, MAX_DAYS)
        if len(recent) > MAX_ITEMS:
            recent = recent[:MAX_ITEMS]
        # If after date filter we have fewer than MAX_ITEMS, fill with older
        # items up to the limit.
        if len(recent) < MAX_ITEMS:
            needed = MAX_ITEMS - len(recent)
            older = [n for n in combined if n not in recent][:needed]
            recent.extend(older)

        news_file.write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Updated {news_file.name}: {len(recent)} items")

    # Cleanup old JSON files
    prune_old_files(NEWS_DIR, days=30)


if __name__ == "__main__":
    main()
