# -*- coding: utf-8 -*-
"""
Portfolio data pipeline
=======================

This script implements the three‑step workflow described in ``cline_rules.md``:

1. **update_portfolio()** – Connect to Interactive Brokers TWS, fetch the latest
   market price for each ticker listed in ``rules.md`` and calculate the
   unrealised profit/loss. The original ``rules.md`` is backed up before being
   overwritten.
2. **download_history()** – Retrieve long‑window OHLCV for ``watchlist.csv`` **plus**
   benchmark **SPY** (S&P 500 ETF). Primary source is TWS; fallback is ``yfinance``.
   Raw data is stored as ``data/raw/{ticker}_ohlcv.csv``.
3. **enrich_data()** – Load the raw OHLCV files, fill ``IV`` with **rolling** 30‑day
   annualised HV (time‑varying), and use ``yfinance`` for analyst fields. The
   enriched file is still named ``data/raw/{ticker}_180d.csv`` (historical name;
   rows may exceed 180). The intermediate ``*_ohlcv.csv`` file is removed.

The script is deliberately serial – each step must finish before the next one
starts – to respect the TWS asynchronous connection requirements outlined in
the rules.
"""

import os
import sys
import asyncio
from pathlib import Path

import numpy as np
import pandas as pd
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]

# 目标约 500 日历日；IB 要求日线 duration 超过 365 天时使用 ``N Y``（见 ``_tws_duration_str``）
OHLCV_CALENDAR_DAYS = 500

# 标普 500 ETF，作大盘基准（与 analyze_report.market_context 一致）
BENCHMARK_SYMBOL = "SPY"


def _pipeline_symbols() -> list[str]:
    """观察清单 + 基准，去重且保持顺序。"""
    out: list[str] = []
    seen: set[str] = set()
    for s in _read_watchlist():
        u = s.strip().upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    if BENCHMARK_SYMBOL not in seen:
        out.append(BENCHMARK_SYMBOL)
    return out


def _tws_duration_str(days: int) -> str:
    """IB Error 321：大于 365 日的历史请求必须用年，例如 ``2 Y``。"""
    if days <= 365:
        return f"{days} D"
    years = max(1, (days + 364) // 365)
    return f"{years} Y"


# ---------------------------------------------------------------------------
# Helper utilities (shared across the three phases)
# ---------------------------------------------------------------------------

def _ensure_ib_connection():
    """Create an ``IB`` instance and connect to the local TWS.

    On Windows the ``ib_insync`` package (which depends on ``eventkit``) expects
    an event‑loop policy to be set *before* the import. We therefore configure
    the ``WindowsSelectorEventLoopPolicy`` and create a default loop prior to
    importing ``IB``. The function returns a tuple ``(ib, use_fallback)`` where
    ``use_fallback`` is ``True`` when the connection could not be established.
    """
    import sys, asyncio
    if sys.platform.startswith('win'):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            # Create a default loop for eventkit to pick up.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        except Exception:
            pass

    from ib_insync import IB

    ib = IB()
    # 优先连局域网 TWS（用户常用电脑 10.10.10.10），再试本地
    for host in ('10.10.10.10', '127.0.0.1'):
        try:
            ib.connect(host, 7496, clientId=66, timeout=10)
            print(f"[OK] TWS 已连接: {host}:7496")
            return ib, False
        except Exception:
            continue
    # 全部失败 → fallback yfinance
    print(f"[WARN] TWS 连接失败 – 将使用 yfinance 作为回退数据源。")
    return None, True


def _read_watchlist(path: str | None = None) -> list:
    """Return a list of ticker symbols from the watchlist CSV.

    The file may span multiple lines (e.g. one ticker per line or multi‑line
    comma‑separated).  All newlines are replaced with commas before splitting.
    Paths are resolved relative to the project root so scripts work from any cwd.
    """
    p = Path(path) if path else ROOT / "data" / "watchlist.csv"
    if not p.is_absolute():
        p = ROOT / p
    try:
        text = p.read_text(encoding='utf-8').strip()
        # Normalise newlines to commas so multi‑line CSVs work transparently
        text = text.replace('\r\n', ',').replace('\n', ',').replace('\r', ',')
        return [sym.strip().upper() for sym in text.split(',') if sym.strip()]
    except Exception as e:
        print(f"[ERR] 读取 watchlist 失败: {e}")
        return []


def _extract_tickers_from_rules(path: str | None = None) -> list:
    """Parse ``rules.md`` and return a list of ticker symbols present in the
    holdings table.
    """
    import re
    tickers = []
    p = Path(path) if path else ROOT / "rules.md"
    if not p.is_absolute():
        p = ROOT / p
    try:
        with open(p, 'r', encoding='utf-8') as f:
            for line in f:
                # Table rows start with a pipe and have the ticker as the first
                # non‑empty cell after the leading pipe.
                if line.startswith('|'):
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) > 2 and parts[1]:
                        tickers.append(parts[1])
        return tickers
    except Exception as e:
        print(f"[ERR] 读取 rules.md 失败: {e}")
        return []


# ---------------------------------------------------------------------------
# Phase 1 – update_portfolio()
# ---------------------------------------------------------------------------

def update_portfolio():
    """使用 TWS 实时持仓信息（``ib.portfolio()``）重新生成 ``rules.md`` 中的持仓表。

    该函数不再依赖 ``rules.md`` 中的旧表格来决定需要更新的代码，而是直接从 TWS 获取
    当前持仓、持仓成本、最新市价以及每股盈亏，并完整覆盖原有的持仓表格，确保文件内容
    与实盘 100% 一致。
    """
    # 连接 IB TWS，若连接失败则直接退出（因为任务要求必须使用实盘持仓）
    ib, fallback = _ensure_ib_connection()
    if fallback or ib is None:
        print('[WARN] 无法连接 TWS，已跳过 rules.md 更新。')
        return

    # 从 TWS 获取持仓列表
    try:
        portfolio = ib.portfolio()
    except Exception as e:
        print(f'[WARN] 获取 TWS 持仓失败: {e}')
        ib.disconnect()
        return

    # 组织持仓信息，键为 ticker，值为 dict
    holdings = {}
    for pos in portfolio:
        # ``pos`` 为 ``Position`` 对象，包含 contract、position、marketPrice、averageCost 等属性
        symbol = getattr(pos.contract, 'symbol', None)
        if not symbol:
            continue
        holdings[symbol] = {
            'position': getattr(pos, 'position', 0),
            'averageCost': getattr(pos, 'averageCost', 0.0),
            'marketPrice': getattr(pos, 'marketPrice', 0.0),
        }

    ib.disconnect()

    # 写入独立的持仓文件：data/holdings/holdings.json
    holdings_dir = ROOT / "data" / "holdings"
    holdings_dir.mkdir(parents=True, exist_ok=True)
    holdings_path = holdings_dir / "holdings.json"

    out = {
        "meta": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "source": "tws",
        },
        "holdings": holdings,
    }
    try:
        import json

        holdings_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] 已将实时持仓写入 {holdings_path.as_posix()}")
    except Exception as e:
        print(f"[ERR] 写入持仓文件失败: {e}")


# ---------------------------------------------------------------------------
# Phase 2 – download_history()
# ---------------------------------------------------------------------------

def _download_ohlcv_tws(ib, symbol: str, days: int = 180) -> pd.DataFrame | None:
    """Attempt to download OHLCV data via TWS. Returns ``None`` on failure.
    """
    from ib_insync import Stock, util
    try:
        contract = Stock(symbol, 'SMART', 'USD')
        duration = _tws_duration_str(days)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime='',
            durationStr=duration,
            barSizeSetting='1 day',
            whatToShow='TRADES',
            useRTH=True,
            formatDate=1,
        )
        ib.sleep(2)
        if bars is None:
            return None
        df = util.df(bars)
        if df is None or df.empty:
            return None
        df = df[['date', 'open', 'high', 'low', 'close', 'volume']].copy()
        df.rename(columns={'date': 'Date'}, inplace=True)
        df['Date'] = pd.to_datetime(df['Date'])
        return df
    except Exception as e:
        print(f"[WARN] TWS 下载 {symbol} OHLCV 失败: {e}")
        return None


def _download_ohlcv_yf(symbol: str, days: int = 180) -> pd.DataFrame | None:
    """Download OHLCV using yfinance as a fallback.
    """
    import yfinance as yf
    try:
        end = datetime.now()
        start = end - timedelta(days=days)
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start, end=end, interval='1d')
        if hist.empty:
            return None
        df = hist[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        df = df.reset_index()
        # yfinance 索引列名因版本而异（Date / Datetime / 无名）
        first_col = df.columns[0]
        if first_col != "Date":
            df.rename(columns={first_col: "Date"}, inplace=True)
        df["Date"] = pd.to_datetime(df["Date"])
        if getattr(df["Date"].dt, "tz", None) is not None:
            df["Date"] = df["Date"].dt.tz_convert(None)
        return df
    except Exception as e:
        print(f"[WARN] yfinance 下载 {symbol} OHLCV 失败: {e}")
        return None


def download_history():
    """Download long‑window OHLCV for watchlist **and** benchmark ``SPY``.
    The result is stored as ``data/raw/{ticker}_ohlcv.csv``.
    """
    symbols = _pipeline_symbols()
    if not symbols:
        print('[WARN] watchlist 为空且未加入基准，终止下载。')
        return

    days = OHLCV_CALENDAR_DAYS
    print(f"[INFO] 历史行情下载窗口: {days} 个日历日（含 MA200 / 模拟用样本）")

    ib, fallback = _ensure_ib_connection()
    for sym in symbols:
        df = None
        if not fallback:
            df = _download_ohlcv_tws(ib, sym, days=days)
        if df is None:
            df = _download_ohlcv_yf(sym, days=days)
        if df is None:
            print(f"[ERR] 无法获取 {sym} 的历史数据，跳过。")
            continue
        out_path = ROOT / "data" / "raw" / f"{sym}_ohlcv.csv"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)
        print(f"[OK] 已保存 {sym} 的原始 OHLCV 至 {out_path.as_posix()}")
    if ib:
        ib.disconnect()


# ---------------------------------------------------------------------------
# Phase 3 – enrich_data()
# ---------------------------------------------------------------------------

def _rolling_annualized_hv(close: pd.Series, window: int = 30) -> pd.Series:
    """日频收盘价 → 滚动 window 日对数收益年化波动率（与 analyze 中 IV 列语义一致）。"""
    log_ret = np.log(close.astype(float) / close.astype(float).shift(1))
    return log_ret.rolling(window, min_periods=window).std() * (252 ** 0.5)


def enrich_data():
    """Enrich the raw OHLCV files with IV (30‑day HV) and analyst data.
    The final CSV is written as ``data/raw/{ticker}_180d.csv`` and the
    intermediate ``*_ohlcv.csv`` file is removed.
    """
    import yfinance as yf
    symbols = _pipeline_symbols()
    for sym in symbols:
        raw_path = ROOT / "data" / "raw" / f"{sym}_ohlcv.csv"
        if not raw_path.is_file():
            print(f"[WARN] 原始文件 {raw_path.as_posix()} 不存在，跳过 {sym} 的补全。")
            continue
        df = pd.read_csv(raw_path)
        # Ensure Date column is datetime
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'])
        else:
            # Some sources may use lowercase 'date'
            df.rename(columns=lambda c: c.title() if c.lower() == 'date' else c, inplace=True)
            df['Date'] = pd.to_datetime(df['Date'])
        # Determine the column name for closing price (case‑insensitive)
        close_col = None
        for cand in ['Close', 'close', 'CLOSE']:
            if cand in df.columns:
                close_col = cand
                break
        if close_col is None:
            print(f"[WARN] {sym} 的原始文件缺少收盘价列，跳过 HV 计算。")
            df["IV"] = np.nan
        else:
            df['IV'] = _rolling_annualized_hv(df[close_col], window=30)
        # Fetch analyst data via yfinance
        try:
            ticker = yf.Ticker(sym)
            info = ticker.info
            target = info.get('targetMeanPrice')
            rating = info.get('recommendationKey')
            df['AnalystTargetPrice'] = target if target is not None else pd.NA
            df['AnalystRating'] = rating if rating is not None else pd.NA
        except Exception as e:
            print(f"[WARN] 获取 {sym} 分析师数据失败: {e}")
            df['AnalystTargetPrice'] = pd.NA
            df['AnalystRating'] = pd.NA
        out_path = ROOT / "data" / "raw" / f"{sym}_180d.csv"
        df.to_csv(out_path, index=False)
        print(f"[OK] 已生成完整 CSV: {out_path.as_posix()}")
        # 删除临时文件
        try:
            raw_path.unlink()
            print(f"[OK] 已删除临时文件 {raw_path.as_posix()}")
        except Exception as e:
            print(f"[WARN] 删除临时文件失败: {e}")


def main():
    update_portfolio()
    download_history()
    enrich_data()
    # ---------------------------------------------------------------------
    # 自动入库最近生成的量化指标 JSON（位于 data/analysis/）
    # ---------------------------------------------------------------------
    try:
        import json
        import sqlite3
        from pathlib import Path

        analysis_dir = Path(__file__).resolve().parents[1] / "data" / "analysis"
        db_path = Path(__file__).resolve().parents[1] / "investment_lab.db"
        conn = sqlite3.connect(db_path)
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
        for json_file in analysis_dir.glob("*_metrics.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                meta = data.get("meta", {})
                ticker = meta.get("ticker")
                date = meta.get("history_last_date")
                if ticker and date:
                    cur.execute(
                        "INSERT OR REPLACE INTO quantitative (ticker, date, metrics) VALUES (?, ?, ?)",
                        (ticker, date, json.dumps(data, ensure_ascii=False)),
                    )
            except Exception as e:
                print(f"[WARN] 入库 {json_file.name} 失败: {e}")
        conn.commit()
    except Exception as e:  # pragma: no cover
        print(f"[WARN] 自动入库过程出现异常: {e}")
    finally:
        if 'conn' in locals():
            conn.close()


if __name__ == '__main__':
    main()
