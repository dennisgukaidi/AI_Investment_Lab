#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_pipeline_and_reports.py — 一键跑通“更新数据 + 生成报告”（支持不开 TWS）
=========================================================

默认行为（推荐）：
1) 运行 portfolio_pipeline.py
   - 尝试连接 TWS；若失败会自动回退到 yfinance 下载行情
   - 输出：data/raw/*_180d.csv（以及 data/holdings/holdings.json 若能连接 TWS）
2) 运行 news_collector.py
   - 输出：data/news/*_news.json
3) 对「持仓 + watchlist」逐个生成 strategy_advisor 报告
   - 报告来自 investment_lab.db 的 quantitative 表（已有 metrics）
   - 输出：reports/strategy_{TICKER}_{YYYYMMDD}.md
4)（可选）生成一份持仓摘要 reports/holdings_summary_{YYYYMMDD}.md

重要说明：
- 本脚本不会“计算新的 *_metrics.json”。如果你需要更新 quantitative/metrics，
  需要先生成 data/analysis/*_metrics.json（例如由 analyze_report.py 产出），
  再用 import_analysis_to_db.py / _db_ingest_helper 写入 DB。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _unique(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in items:
        s = (s or "").strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _read_holdings_tickers() -> list[str]:
    p = PROJECT_ROOT / "data" / "holdings" / "holdings.json"
    if not p.is_file():
        return []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        holdings = obj.get("holdings", {}) or {}
        return sorted(_unique(holdings.keys()))
    except Exception:
        return []


def _read_watchlist_tickers() -> list[str]:
    p = PROJECT_ROOT / "data" / "watchlist.csv"
    if not p.is_file():
        return []
    line = p.read_text(encoding="utf-8").strip()
    if not line:
        return []
    return _unique(line.split(","))


def _run(args: list[str]) -> None:
    # 统一在项目根目录执行，避免 cwd 导致的相对路径问题
    print(f"[RUN] {' '.join(args)}")
    subprocess.check_call(args, cwd=str(PROJECT_ROOT))


def _generate_holdings_summary(holdings: list[str], output_dir: Path) -> Path | None:
    db_path = PROJECT_ROOT / "investment_lab.db"
    if not db_path.is_file():
        print("[WARN] investment_lab.db 不存在，跳过持仓摘要生成。")
        return None

    def latest_row(cur: sqlite3.Cursor, ticker: str):
        cur.execute(
            "SELECT date, metrics FROM quantitative WHERE ticker=? ORDER BY date DESC LIMIT 1",
            (ticker,),
        )
        return cur.fetchone()

    lines: list[str] = []
    lines.append("# 持仓摘要（来自 investment_lab.db）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 覆盖标的：{', '.join(holdings) if holdings else '(空)'}")
    lines.append("")

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    for t in holdings:
        row = latest_row(cur, t)
        lines.append(f"## {t}")
        lines.append("")
        if not row:
            lines.append("- 数据库无记录（quantitative 表缺少该 ticker）")
            lines.append("")
            continue

        date, metrics_json = row
        lines.append(f"- 最新日期：{date}")
        try:
            data = json.loads(metrics_json)
        except Exception:
            lines.append("- metrics JSON 解析失败")
            lines.append("")
            continue

        risk = data.get("risk_matrix", {}) or {}
        trend = data.get("trend", {}) or {}
        iv = data.get("iv_analysis", {}) or {}
        bootstrap = (data.get("bootstrap", {}) or {}).get("horizons", {}) or {}
        h60 = bootstrap.get("60", {}) or {}

        def add(name: str, val):
            if val is None:
                return
            lines.append(f"- {name}：{val}")

        add("现价", risk.get("latest_close"))
        add("趋势", trend.get("regime"))
        add("风险/收益评分", risk.get("rr_score"))
        add("1.5×ATR 止损", risk.get("stop_1x5_atr"))
        add("激进目标", risk.get("tp_aggressive"))
        add("IV 分位", iv.get("iv_percentile"))
        add("60d 回本概率（always above cost）", h60.get("prob_paths_always_above_cost"))
        lines.append("")

    conn.close()

    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"holdings_summary_{datetime.now().strftime('%Y%m%d')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] holdings summary saved: {out}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="一键跑通更新数据+新闻+生成策略报告")
    parser.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="跳过 portfolio_pipeline.py（不更新行情/不下载 raw 数据）",
    )
    parser.add_argument(
        "--skip-news",
        action="store_true",
        help="跳过 news_collector.py（不更新新闻/情绪）",
    )
    parser.add_argument(
        "--report-scope",
        choices=["holdings", "watchlist", "both"],
        default="both",
        help="要生成报告的标的范围：仅持仓/仅watchlist/两者都要",
    )
    parser.add_argument(
        "--output-dir",
        default="reports",
        help="报告输出目录（默认：reports）。注意：strategy_advisor 会自动附加日期后缀。",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="额外生成一份持仓摘要（从 DB 读取）到 reports/holdings_summary_YYYYMMDD.md",
    )
    parser.add_argument(
        "--tickers",
        default="",
        help="手动指定 ticker（逗号分隔）。提供后会覆盖 report-scope 的自动选择。",
    )
    args = parser.parse_args()

    py = sys.executable
    output_dir = (PROJECT_ROOT / args.output_dir).resolve()

    if not args.skip_pipeline:
        _run([py, "scripts/portfolio_pipeline.py"])

    if not args.skip_news:
        _run([py, "scripts/news_collector.py"])

    # 选择标的
    holdings = _read_holdings_tickers()
    watchlist = _read_watchlist_tickers()

    if args.tickers.strip():
        tickers = _unique(args.tickers.split(","))
    else:
        if args.report_scope == "holdings":
            tickers = _unique(holdings)
        elif args.report_scope == "watchlist":
            tickers = _unique(watchlist)
        else:
            tickers = _unique(list(holdings) + list(watchlist))

    print(f"[INFO] holdings tickers: {holdings}")
    print(f"[INFO] watchlist tickers: {watchlist}")
    print(f"[INFO] report tickers ({len(tickers)}): {tickers}")

    # 逐个生成报告：output-dir 传目录即可，strategy_advisor 会自动按日期命名
    for t in tickers:
        try:
            _run([py, "scripts/strategy_advisor.py", "--ticker", t, "--output", str(output_dir)])
        except subprocess.CalledProcessError:
            # 个别 ticker DB 无数据时，strategy_advisor 会打印 [ERR] 并返回；
            # 为了流程不中断，这里继续后续。
            print(f"[WARN] 生成 {t} 报告失败，已跳过。")

    if args.summary and holdings:
        _generate_holdings_summary(holdings, output_dir)

    print("[DONE] all finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

