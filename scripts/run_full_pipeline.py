#!/usr/bin/env python3
"""Run full data collection pipeline in the correct order.

Behaviour:
- Read tickers from `data/watchlist.csv`.
- Run collectors (portfolio_pipeline, news_collector, macro, alternative, fundamental).
- Verify per‑ticker outputs exist before running analysis/import/report steps.

This enforces: "所有脚本的ticker都从watchlist里找，数据收集全之前不要运行步骤5-7".
"""
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
DRY_RUN = "--dry-run" in sys.argv


def read_watchlist() -> List[str]:
    p = ROOT / "data" / "watchlist.csv"
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8").strip()
    # Normalise newlines to commas so multi‑line CSVs work transparently
    text = text.replace("\r\n", ",").replace("\n", ",").replace("\r", ",")
    return [s.strip().upper() for s in text.split(",") if s.strip()]


def run_cmd(args: List[str]) -> int:
    print(f">>> Running: {' '.join(args)}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # If running news_collector, force it to use watchlist (avoid relying on holdings.json)
    try:
        if any('news_collector.py' in a for a in args):
            env["FORCE_USE_WATCHLIST"] = "1"
    except Exception:
        pass
    if DRY_RUN:
        print(f"(dry-run) would run in {ROOT}: {' '.join(args)}")
        return 0
    r = subprocess.run(args, cwd=ROOT, env=env)
    print(f"<<< Exit {r.returncode}\n")
    return r.returncode


def file_ok(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def main() -> int:
    tickers = read_watchlist()
    if not tickers:
        print("[ERR] watchlist.csv 为空或不存在")
        return 2

    # 1) Portfolio pipeline (history + enrich)
    if run_cmd([PY, "scripts/portfolio_pipeline.py"]) != 0:
        print("[WARN] portfolio_pipeline 退出码非 0，继续尝试后续步骤")

    # 2) News collector (global, reads watchlist/rules)
    if run_cmd([PY, "scripts/news_collector.py"]) != 0:
        print("[WARN] news_collector 失败（可能为网络/证书问题）")

    # 3) Macroeconomic
    if run_cmd([PY, "scripts/macroeconomic_data_collector.py"]) != 0:
        print("[WARN] macroeconomic_data_collector 失败")

    # 4) Alternative & fundamental per ticker
    for t in tickers:
        if run_cmd([PY, "scripts/alternative_data_collector.py", t]) != 0:
            print(f"[WARN] alternative_data_collector 对 {t} 失败")
        if run_cmd([PY, "scripts/fundamental_data_collector.py", t]) != 0:
            print(f"[WARN] fundamental_data_collector 对 {t} 失败")

    # 5) Verify data presence & build the subset of tickers with complete data
    complete_tickers = []
    incomplete_tickers = []
    for t in tickers:
        raw = ROOT / "data" / "raw" / f"{t}_180d.csv"
        news = ROOT / "data" / "news" / f"{t}_news.json"
        fund = ROOT / "data" / "fundamentals" / f"{t}_fundamentals.json"
        alt = ROOT / "data" / "alternative" / f"{t}_alternative.json"
        if file_ok(raw) and file_ok(news) and file_ok(fund) and file_ok(alt):
            complete_tickers.append(t)
        else:
            missing_items = []
            if not file_ok(raw):
                missing_items.append("raw")
            if not file_ok(news):
                missing_items.append("news")
            if not file_ok(fund):
                missing_items.append("fundamentals")
            if not file_ok(alt):
                missing_items.append("alternative")
            incomplete_tickers.append((t, missing_items))

    if incomplete_tickers:
        print("[WARN] 以下标的缺少数据文件，将被跳过（不影响其他标的）：")
        for t, items in incomplete_tickers:
            print(f" - {t}: 缺少 {', '.join(items)} 数据文件")

    if not complete_tickers:
        print("[ERR] 没有任何标的有完整数据，终止流水线")
        return 3

    print(f"[INFO] 数据完整的标的 ({len(complete_tickers)} 个): {', '.join(complete_tickers)}")

    # 6) Only run analyze on complete tickers
    if run_cmd([PY, "scripts/analyze_report.py", "--all", "--skip-incomplete"]) != 0:
        print("[WARN] analyze_report.py 运行失败（不中止）")

    if run_cmd([PY, "scripts/import_analysis_to_db.py"]) != 0:
        print("[WARN] import_analysis_to_db.py 运行失败（不中止）")

    # 7) 精算回填（默认遍历全部 ticker，静默 UPDATE DB）
    if run_cmd([PY, "scripts/strategy_advisor.py"]) != 0:
        print("[WARN] strategy_advisor.py 运行失败（不中止）")

    # 8) CSV 导出（直接从 ticker_metrics 表读取）
    if run_cmd([PY, "scripts/export_to_csv.py"]) != 0:
        print("[WARN] export_to_csv.py 运行失败（表格可能未更新）")

    # 9) 策略扫描雷达（五模块量化监控，生成 Markdown 报告）
    if run_cmd([PY, "scripts/strategy_radar.py", "--verbose"]) != 0:
        print("[WARN] strategy_radar.py 运行失败")

    print("[OK] 全流程完成（含分析/入库/精算回填/表格导出/策略报告）")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
