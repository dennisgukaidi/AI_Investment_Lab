#!/usr/bin/env python3
"""
=============================================================================
  ticker_metrics 表 → CSV 导出脚本（36 字段）
=============================================================================
  功能：
    直接从 investment_lab.db 的 ticker_metrics 表读取全量数据，导出为 CSV。
    排序规则：先按 Ticker 字母升序，再按 Date 降序（最新在前）。

  用法：
    .venv\\Scripts\\python scripts\\export_to_csv.py

  输出：
    data/ticker_data.csv（UTF-8 BOM，Excel 可直接打开）

=============================================================================
"""

import csv
import pathlib
import sqlite3

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "investment_lab.db"
OUTPUT_CSV = PROJECT_ROOT / "data" / "ticker_data.csv"

# ticker_metrics 表全部 36 列（与 init_db.py 中定义一致）
COLUMNS = [
    "Date",
    "Ticker",
    "Status",
    "Entry_Ref",
    "RR_Ratio",
    "Kelly_Pct",
    "Close_Price",
    "Trend_State",
    "RSI",
    "IV_Rank",
    "Crowding_Index",
    "Crowding_Label",
    "Max_Corr_R2",
    "Breakeven_Days",
    "Win_Prob_10d",
    "Risk_Loss_10d",
    "Target_Prob_10d",
    "Target_Median_10d",
    "Win_Prob_20d",
    "Risk_Loss_20d",
    "Target_Prob_20d",
    "Target_Median_20d",
    "Win_Prob_60d",
    "Risk_Loss_60d",
    "Target_Prob_60d",
    "Target_Median_60d",
    "ATR_14",
    "Hard_Stop_Loss",
    "Target_Aggressive",
    "RR_Score",
    "PE_Percentile",
    "IV_Status",
    "SPY_State",
    "Alpha_vs_SPY",
    "Corr_vs_SPY",
    "Action",
]


def export_ticker_metrics(db_path: pathlib.Path, output_path: pathlib.Path) -> int:
    """从 ticker_metrics 表读出全量数据，写入 CSV。返回行数。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    columns_str = ", ".join(f'"{c}"' for c in COLUMNS)
    cur = conn.execute(
        f"SELECT {columns_str} FROM ticker_metrics ORDER BY Ticker ASC, Date DESC"
    )
    rows = cur.fetchall()
    conn.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow(list(row))

    return len(rows)


def main() -> None:
    if not DB_PATH.is_file():
        print(f"[ERR] 数据库不存在: {DB_PATH}")
        return

    count = export_ticker_metrics(DB_PATH, OUTPUT_CSV)
    print(f"[OK] 已从 ticker_metrics 表导出 {count} 行 → {OUTPUT_CSV}")


if __name__ == "__main__":
    main()