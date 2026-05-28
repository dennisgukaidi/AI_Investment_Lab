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
    line = p.read_text(encoding="utf-8").strip()
    return [s.strip().upper() for s in line.split(",") if s.strip()]


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

    # 5) Verify data presence for all tickers
    missing = []
    for t in tickers:
        raw = ROOT / "data" / "raw" / f"{t}_180d.csv"
        news = ROOT / "data" / "news" / f"{t}_news.json"
        fund = ROOT / "data" / "fundamentals" / f"{t}_fundamentals.json"
        alt = ROOT / "data" / "alternative" / f"{t}_alternative.json"
        if not file_ok(raw):
            missing.append((t, str(raw)))
        if not file_ok(news):
            missing.append((t, str(news)))
        if not file_ok(fund):
            missing.append((t, str(fund)))
        if not file_ok(alt):
            missing.append((t, str(alt)))

    if missing:
        print("[ERR] 检测到缺失数据文件，分析/入库/报告将被跳过：")
        for t, p in missing:
            print(f" - {t}: missing {p}")
        return 3

    # 6) All data present → run analyze, import, reports
    if run_cmd([PY, "scripts/analyze_report.py", "--all"]) != 0:
        print("[ERR] analyze_report.py 运行失败")
        return 4
    if run_cmd([PY, "scripts/import_analysis_to_db.py"]) != 0:
        print("[ERR] import_analysis_to_db.py 运行失败")
        return 5
    for t in tickers:
        out_dir = ROOT / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        if run_cmd([PY, "scripts/strategy_advisor.py", "--ticker", t, "--output", str(out_dir)]) != 0:
            print(f"[ERR] strategy_advisor.py 对 {t} 运行失败")
            return 6
    # 8) 更新表格导出（export_to_csv.py）
    if run_cmd([PY, "scripts/export_to_csv.py"]) != 0:
        print("[WARN] export_to_csv.py 运行失败（表格可能未更新）")

    print("[OK] 全流程完成（含分析/入库/生成报告/表格导出）")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
