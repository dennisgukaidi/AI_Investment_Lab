from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
from datetime import datetime
from typing import Iterable

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "investment_lab.db"
ANALYSIS_DIR = BASE_DIR / "data" / "analysis"
FUNDAMENTALS_DIR = BASE_DIR / "data" / "fundamentals"
NEWS_DIR = BASE_DIR / "data" / "news"
MACRO_FILE = BASE_DIR / "data" / "macroeconomic" / "macro_data.json"
WATCHLIST_FILE = BASE_DIR / "data" / "watchlist.csv"

# 引入 strategy_advisor 中的核心类用于 ticker_metrics 写入
sys.path.insert(0, str(BASE_DIR / "scripts"))
try:
    from strategy_advisor import (
        EnhancedDataRepository,
        EnhancedStrategyAdvisor,
    )
    _HAS_ADVISOR = True
except Exception:
    _HAS_ADVISOR = False


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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ticker_metrics (
            Date                TEXT NOT NULL,
            Ticker              TEXT NOT NULL,
            Status              TEXT,
            Entry_Ref           REAL,
            RR_Ratio            TEXT,
            Kelly_Pct           REAL,
            Close_Price         REAL,
            Trend_State         TEXT,
            RSI                 REAL,
            IV_Rank             REAL,
            Crowding_Index      REAL,
            Crowding_Label      TEXT,
            Max_Corr_R2         REAL,
            Breakeven_Days      REAL,
            Win_Prob_10d        REAL,
            Risk_Loss_10d       REAL,
            Target_Prob_10d     REAL,
            Target_Median_10d   REAL,
            Win_Prob_20d        REAL,
            Risk_Loss_20d       REAL,
            Target_Prob_20d     REAL,
            Target_Median_20d   REAL,
            Win_Prob_60d        REAL,
            Risk_Loss_60d       REAL,
            Target_Prob_60d     REAL,
            Target_Median_60d   REAL,
            ATR_14              REAL,
            Hard_Stop_Loss      REAL,
            Target_Aggressive   REAL,
            RR_Score            REAL,
            PE_Percentile       REAL,
            IV_Status           TEXT,
            SPY_State           TEXT,
            Alpha_vs_SPY        REAL,
            Corr_vs_SPY         REAL,
            Action              TEXT,
            PRIMARY KEY (Date, Ticker)
        )
        """
    )


def _sync_ticker_metrics(conn: sqlite3.Connection) -> int:
    """从 quantitative 表读取最新 metrics，二次计算后写入 ticker_metrics 表。返回写入条数。"""
    if not _HAS_ADVISOR:
        print("[SKIP] ticker_metrics: strategy_advisor 导入失败，跳过同步")
        return 0

    cur = conn.cursor()
    cur.execute(
        "SELECT ticker, date, metrics FROM quantitative ORDER BY date DESC"
    )
    rows = cur.fetchall()

    repo = EnhancedDataRepository(DB_PATH)
    written = 0

    for row in rows:
        ticker = row["ticker"]
        date = row["date"]
        try:
            data = json.loads(row["metrics"])
            snapshot = repo._parse_metrics_to_snapshot(ticker, date, data)
        except Exception as exc:
            print(f"[WARN] ticker_metrics parse failed for {ticker} {date}: {exc}")
            continue

        try:
            advisor = EnhancedStrategyAdvisor(snapshot, repo=repo)
            metrics_row = advisor.build_metrics_row()

            cur.execute(
                """
                INSERT OR REPLACE INTO ticker_metrics (
                    Date, Ticker, Status, Entry_Ref, RR_Ratio, Kelly_Pct,
                    Close_Price, Trend_State, RSI, IV_Rank, Crowding_Index,
                    Crowding_Label, Max_Corr_R2, Breakeven_Days,
                    Win_Prob_10d, Risk_Loss_10d, Target_Prob_10d, Target_Median_10d,
                    Win_Prob_20d, Risk_Loss_20d, Target_Prob_20d, Target_Median_20d,
                    Win_Prob_60d, Risk_Loss_60d, Target_Prob_60d, Target_Median_60d,
                    ATR_14, Hard_Stop_Loss, Target_Aggressive, RR_Score,
                    PE_Percentile, IV_Status, SPY_State, Alpha_vs_SPY, Corr_vs_SPY,
                    Action
                ) VALUES (
                    :Date, :Ticker, :Status, :Entry_Ref, :RR_Ratio, :Kelly_Pct,
                    :Close_Price, :Trend_State, :RSI, :IV_Rank, :Crowding_Index,
                    :Crowding_Label, :Max_Corr_R2, :Breakeven_Days,
                    :Win_Prob_10d, :Risk_Loss_10d, :Target_Prob_10d, :Target_Median_10d,
                    :Win_Prob_20d, :Risk_Loss_20d, :Target_Prob_20d, :Target_Median_20d,
                    :Win_Prob_60d, :Risk_Loss_60d, :Target_Prob_60d, :Target_Median_60d,
                    :ATR_14, :Hard_Stop_Loss, :Target_Aggressive, :RR_Score,
                    :PE_Percentile, :IV_Status, :SPY_State, :Alpha_vs_SPY, :Corr_vs_SPY,
                    :Action
                )
                """,
                metrics_row,
            )
            written += 1
        except Exception as exc:
            print(f"[WARN] ticker_metrics write failed for {ticker} {date}: {exc}")

    conn.commit()
    return written


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

    # 同步写入 ticker_metrics 表（需要先 commit quantitative 数据使其可见）
    imported_tm = _sync_ticker_metrics(conn)

    conn.close()
    print(
        f"Imported quantitative={imported_quant}, fundamentals={imported_fund}, "
        f"sentiment={imported_sent}, macro={imported_macro}, "
        f"ticker_metrics={imported_tm}"
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
