"""import_analysis_to_db.py — 将 data/analysis 中的 *_metrics.json 导入 SQLite

此脚本在项目根目录下运行，读取 ``data/analysis`` 目录下的所有量化指标 JSON，
并写入 ``investment_lab.db`` 的 ``quantitative`` 表。若记录已存在则使用 ``INSERT OR REPLACE``
进行更新。脚本可多次运行，重复导入时不会产生冲突。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import datetime

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "investment_lab.db"
ANALYSIS_DIR = BASE_DIR / "data" / "analysis"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def import_analysis() -> None:
    conn = _connect()
    cur = conn.cursor()
    # 确保表存在（防止用户未运行 init_db）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quantitative (
            ticker  TEXT NOT NULL,
            date    TEXT NOT NULL,
            metrics TEXT NOT NULL,
            PRIMARY KEY (ticker, date)
        )
        """
    )

    for json_file in ANALYSIS_DIR.glob("*_metrics.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            meta = data.get("meta", {})
            ticker = meta.get("ticker")
            date = meta.get("history_last_date")
            if not ticker or not date:
                continue
            cur.execute(
                "INSERT OR REPLACE INTO quantitative (ticker, date, metrics) VALUES (?, ?, ?)",
                (ticker, date, json.dumps(data, ensure_ascii=False)),
            )
        except Exception as exc:  # pragma: no cover
            print(f"[WARN] 导入 {json_file.name} 失败: {exc}")
    conn.commit()
    conn.close()
    print(f"已导入 {len(list(ANALYSIS_DIR.glob('*_metrics.json')))} 条量化指标记录")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="导入 data/analysis/*.json 到 SQLite")
    parser.add_argument("--dry-run", action="store_true", help="仅打印将要导入的记录数量，不写入数据库")
    args = parser.parse_args()
    if args.dry_run:
        count = len(list(ANALYSIS_DIR.glob("*_metrics.json")))
        print(f"[DRY RUN] 将导入 {count} 条记录")
    else:
        import_analysis()