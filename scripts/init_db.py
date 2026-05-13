"""init_db.py — 初始化 SQLite 数据库用于投资实验室

此脚本在项目根目录下创建 ``investment_lab.db``（如果尚未存在），并创建四个表：

1. ``fundamentals``   – 保存基本面数据的 JSON，主键为 ``ticker`` + ``date``
2. ``quantitative``   – 保存量化指标（即 ``analyze_report`` 生成的 metrics），主键为 ``ticker`` + ``date``
3. ``macro``          – 保存宏观数据的 JSON，主键为 ``date``（全局宏观数据与股票无关）
4. ``sentiment``      – 保存舆情得分，主键为 ``ticker`` + ``date``

所有 JSON 列均使用 SQLite 的 ``TEXT`` 类型存储，读取时可通过 ``json.loads`` 解析。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import datetime, timedelta

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "investment_lab.db"


def _connect() -> sqlite3.Connection:
    """返回一个到 ``investment_lab.db`` 的连接，若文件不存在会自动创建。"""
    conn = sqlite3.connect(DB_PATH)
    # 为了在后续可以直接存取 JSON，开启行工厂
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """创建所需的表结构（如果尚未存在）。"""
    conn = _connect()
    cur = conn.cursor()

    # 基本面表
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

    # 量化指标表（来自 analyze_report）
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

    # 宏观数据表（全局）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS macro (
            date TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
        """
    )

    # 舆情得分表
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

    conn.commit()
    conn.close()


def clean_old_quantitative(ticker: str) -> None:
    """删除指定 ticker 在 quantitative 表中超过两年的历史记录。"""
    conn = _connect()
    cur = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=730)).date().isoformat()
    cur.execute(
        "DELETE FROM quantitative WHERE ticker=? AND date < ?",
        (ticker, cutoff),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="初始化或清理 investment_lab.db")
    parser.add_argument("--init", action="store_true", help="创建数据库及表结构（默认）")
    parser.add_argument("--clean", action="store_true", help="清理超过 2 年的历史数据")
    args = parser.parse_args()

    if args.clean:
        # 清理旧数据
        conn = _connect()
        cur = conn.cursor()
        cutoff = (datetime.now() - timedelta(days=730)).date().isoformat()  # 2 年前
        tables = ["fundamentals", "quantitative", "macro", "sentiment"]
        for tbl in tables:
            # 对于 macro 表仅有 date 列，其他表有 ticker+date
            if tbl == "macro":
                cur.execute(f"DELETE FROM {tbl} WHERE date < ?", (cutoff,))
            else:
                cur.execute(f"DELETE FROM {tbl} WHERE date < ?", (cutoff,))
        conn.commit()
        conn.close()
        print(f"已清理 {cutoff} 之前的历史数据")
    else:
        init_db()
        print(f"SQLite DB 已初始化: {DB_PATH}")
