from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
from datetime import datetime
from typing import Iterable

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "investment_lab.db"
ANALYSIS_DIR = BASE_DIR / "data" / "analysis"
FUNDAMENTALS_DIR = BASE_DIR / "data" / "fundamentals"
NEWS_DIR = BASE_DIR / "data" / "news"
MACRO_FILE = BASE_DIR / "data" / "macroeconomic" / "macro_data.json"
WATCHLIST_FILE = BASE_DIR / "data" / "watchlist.csv"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _read_watchlist() -> set[str]:
    if not WATCHLIST_FILE.is_file():
        return set()
    raw = WATCHLIST_FILE.read_text(encoding="utf-8").strip()
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def _safe_date(v: str | None) -> str:
    if not v:
        return datetime.now().date().isoformat()
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def _avg_sentiment(items: Iterable[dict]) -> float:
    vals: list[float] = []
    for x in items:
        try:
            if x.get("sentiment_polarity") is not None:
                vals.append(float(x["sentiment_polarity"]))
        except Exception:
            continue
    return float(sum(vals) / len(vals)) if vals else 0.0


def _ensure_tables(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quantitative (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker  TEXT NOT NULL,
            date    TEXT NOT NULL,
            metrics TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,
            data   TEXT NOT NULL,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sentiment (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,
            score  REAL NOT NULL,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS macro (
            date TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
        """
    )


def import_all() -> None:
    conn = _connect()
    cur = conn.cursor()
    _ensure_tables(cur)
    watchlist = _read_watchlist()

    imported_quant = 0
    for json_file in ANALYSIS_DIR.glob("*_metrics.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            meta = data.get("meta", {})
            ticker = str(meta.get("ticker", "")).upper()
            date = meta.get("history_last_date")
            if not ticker or not date:
                continue
            if watchlist and ticker not in watchlist:
                continue
            cur.execute(
                "INSERT INTO quantitative (ticker, date, metrics) VALUES (?, ?, ?)",
                (ticker, date, json.dumps(data, ensure_ascii=False)),
            )
            imported_quant += 1
        except Exception as exc:
            print(f"[WARN] quantitative {json_file.name} failed: {exc}")

    imported_fund = 0
    for json_file in FUNDAMENTALS_DIR.glob("*_fundamentals.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            ticker = str(data.get("ticker", "")).upper() or json_file.stem.replace("_fundamentals", "").upper()
            if watchlist and ticker not in watchlist:
                continue
            date = _safe_date(data.get("collected_at"))
            cur.execute(
                "INSERT OR REPLACE INTO fundamentals (ticker, date, data) VALUES (?, ?, ?)",
                (ticker, date, json.dumps(data, ensure_ascii=False)),
            )
            imported_fund += 1
        except Exception as exc:
            print(f"[WARN] fundamentals {json_file.name} failed: {exc}")

    imported_sent = 0
    for json_file in NEWS_DIR.glob("*_news.json"):
        try:
            ticker = json_file.stem.replace("_news", "").upper()
            if watchlist and ticker not in watchlist:
                continue
            items = json.loads(json_file.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                continue
            score = _avg_sentiment(items)
            # Use file modification date as collection date so sentiment reflects latest run date.
            date = datetime.fromtimestamp(json_file.stat().st_mtime).date().isoformat()
            cur.execute(
                "INSERT OR REPLACE INTO sentiment (ticker, date, score) VALUES (?, ?, ?)",
                (ticker, date, score),
            )
            imported_sent += 1
        except Exception as exc:
            print(f"[WARN] sentiment {json_file.name} failed: {exc}")

    imported_macro = 0
    if MACRO_FILE.is_file():
        try:
            data = json.loads(MACRO_FILE.read_text(encoding="utf-8"))
            date = _safe_date(data.get("collected_at"))
            cur.execute(
                "INSERT OR REPLACE INTO macro (date, data) VALUES (?, ?)",
                (date, json.dumps(data, ensure_ascii=False)),
            )
            imported_macro = 1
        except Exception as exc:
            print(f"[WARN] macro import failed: {exc}")

    conn.commit()
    conn.close()
    print(
        f"Imported quantitative={imported_quant}, fundamentals={imported_fund}, "
        f"sentiment={imported_sent}, macro={imported_macro}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import analysis/fundamentals/sentiment/macro into SQLite")
    parser.add_argument("--dry-run", action="store_true", help="Only show counts, no DB write")
    args = parser.parse_args()

    if args.dry_run:
        print(f"analysis files: {len(list(ANALYSIS_DIR.glob('*_metrics.json')))}")
        print(f"fundamentals files: {len(list(FUNDAMENTALS_DIR.glob('*_fundamentals.json')))}")
        print(f"news files: {len(list(NEWS_DIR.glob('*_news.json')))}")
        print(f"macro file exists: {MACRO_FILE.is_file()}")
    else:
        import_all()
