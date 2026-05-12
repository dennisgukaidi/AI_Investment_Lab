# -*- coding: utf-8 -*-
"""
Portfolio data pipeline
=======================

This script implements the three‑step workflow described in ``cline_rules.md``:

1. **update_portfolio()** – Connect to Interactive Brokers TWS, fetch the latest
   market price for each ticker listed in ``rules.md`` and calculate the
   unrealised profit/loss. The original ``rules.md`` is backed up before being
   overwritten.
2. **download_history()** – Retrieve 180‑day OHLCV data for every ticker in
   ``data/watchlist.csv``. The primary source is the TWS API; if the request
   fails or returns no data the function falls back to ``yfinance``. The raw
   data (only price/volume) is stored as ``data/raw/{ticker}_ohlcv.csv``.
3. **enrich_data()** – Load the raw OHLCV files, compute a 30‑day historical
   volatility (HV) to fill the ``IV`` column, and use ``yfinance`` to obtain the
   analyst target price and rating. The enriched dataframe is saved as
   ``data/raw/{ticker}_180d.csv`` and the intermediate ``*_ohlcv.csv`` file is
   removed.

The script is deliberately serial – each step must finish before the next one
starts – to respect the TWS asynchronous connection requirements outlined in
the rules.
"""

import os
import sys
import asyncio
import pandas as pd
from datetime import datetime, timedelta

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
    try:
        ib.connect('127.0.0.1', 7496, clientId=10)
        return ib, False
    except Exception as e:
        print(f"⚠️ TWS 连接失败: {e} – 将使用 yfinance 作为回退数据源。")
        return None, True


def _read_watchlist(path: str = 'data/watchlist.csv') -> list:
    """Return a list of ticker symbols from the watchlist CSV.

    The file is a single line of comma‑separated symbols.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            line = f.read().strip()
        return [sym.strip() for sym in line.split(',') if sym.strip()]
    except Exception as e:
        print(f"✗ 读取 watchlist 失败: {e}")
        return []


def _extract_tickers_from_rules(path: str = 'rules.md') -> list:
    """Parse ``rules.md`` and return a list of ticker symbols present in the
    holdings table.
    """
    import re
    tickers = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                # Table rows start with a pipe and have the ticker as the first
                # non‑empty cell after the leading pipe.
                if line.startswith('|'):
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) > 2 and parts[1]:
                        tickers.append(parts[1])
        return tickers
    except Exception as e:
        print(f"✗ 读取 rules.md 失败: {e}")
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
        print('⚠️ 无法连接 TWS，已跳过 rules.md 更新。')
        return

    # 从 TWS 获取持仓列表
    try:
        portfolio = ib.portfolio()
    except Exception as e:
        print(f'⚠️ 获取 TWS 持仓失败: {e}')
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

    # 备份原始 rules.md
    if os.path.exists('rules.md'):
        os.replace('rules.md', 'rules.md.bak')

    # 读取原文件，定位表格起止位置，以便保留表格前后的其他内容
    with open('rules.md.bak', 'r', encoding='utf-8') as src:
        lines = src.readlines()

    # 找到表格开始（标题行）和结束（下一个二级标题或文件结束）
    table_start = None
    table_end = None
    for idx, line in enumerate(lines):
        if line.strip().startswith('| 股票代码'):
            table_start = idx
        elif table_start is not None and line.strip().startswith('##'):
            table_end = idx
            break
    if table_start is None:
        print('⚠️ 未在 rules.md 中找到持仓表格起始行，放弃更新。')
        return
    if table_end is None:
        table_end = len(lines)

    # 生成新的表格内容，包含持仓股数、持仓成本、最新市价、每股盈亏
    header = "| 股票代码 | 持仓股数 | 持仓成本 | 市价 | 每股盈亏 | 备注 |\n"
    separator = "|---|---|---|---|---|---|\n"
    rows = []
    for symbol, data in holdings.items():
        position = data['position']
        avg_cost = data['averageCost']
        market_price = data['marketPrice']
        # 每股盈亏 = 市价 - 持仓成本
        pl_per_share = market_price - avg_cost if market_price is not None else float('nan')
        row = f"| {symbol} | {position} | ${avg_cost:.2f} | ${market_price:.2f} | ${pl_per_share:.2f} |  |\n"
        rows.append(row)

    new_table = [header, separator] + rows

    # 重新写入文件：表格前的内容 + 新表格 + 表格后的内容
    with open('rules.md', 'w', encoding='utf-8') as dst:
        # 前半部分（包括表格标题行之前的所有行）
        dst.writelines(lines[:table_start])
        # 写入新表格
        dst.writelines(new_table)
        # 写入表格之后的其余内容（如果有的话）
        dst.writelines(lines[table_end:])

    print('✅ 已使用 TWS 实时持仓更新 rules.md（已备份为 rules.md.bak）')


# ---------------------------------------------------------------------------
# Phase 2 – download_history()
# ---------------------------------------------------------------------------

def _download_ohlcv_tws(ib, symbol: str, days: int = 180) -> pd.DataFrame | None:
    """Attempt to download OHLCV data via TWS. Returns ``None`` on failure.
    """
    from ib_insync import Stock, util
    try:
        contract = Stock(symbol, 'SMART', 'USD')
        duration = f"{days} D"
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
        df = util.df(bars)
        if df.empty:
            return None
        df = df[['date', 'open', 'high', 'low', 'close', 'volume']].copy()
        df.rename(columns={'date': 'Date'}, inplace=True)
        df['Date'] = pd.to_datetime(df['Date'])
        return df
    except Exception as e:
        print(f"⚠️ TWS 下载 {symbol} OHLCV 失败: {e}")
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
        df.reset_index(inplace=True)
        df.rename(columns={'Date': 'Date'}, inplace=True)
        return df
    except Exception as e:
        print(f"⚠️ yfinance 下载 {symbol} OHLCV 失败: {e}")
        return None


def download_history():
    """Download 180‑day OHLCV for every ticker in ``watchlist.csv``.
    The result is stored as ``data/raw/{ticker}_ohlcv.csv``.
    """
    symbols = _read_watchlist()
    if not symbols:
        print('⚠️ watchlist 为空，终止下载。')
        return

    ib, fallback = _ensure_ib_connection()
    for sym in symbols:
        df = None
        if not fallback:
            df = _download_ohlcv_tws(ib, sym)
        if df is None:
            df = _download_ohlcv_yf(sym)
        if df is None:
            print(f"✗ 无法获取 {sym} 的历史数据，跳过。")
            continue
        out_path = f"data/raw/{sym}_ohlcv.csv"
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        df.to_csv(out_path, index=False)
        print(f"✓ 已保存 {sym} 的原始 OHLCV 至 {out_path}")
    if ib:
        ib.disconnect()


# ---------------------------------------------------------------------------
# Phase 3 – enrich_data()
# ---------------------------------------------------------------------------

def _calc_hv(series: pd.Series, window: int = 30) -> float:
    """Calculate annualised historical volatility from a price series.
    ``series`` should be the closing prices. The function returns the volatility
    as a decimal (e.g., 0.25 for 25%).
    """
    import numpy as np
    log_ret = np.log(series / series.shift(1)).dropna()
    if len(log_ret) < window:
        return float('nan')
    vol = log_ret[-window:].std() * (252 ** 0.5)  # annualise assuming 252 trading days
    return vol


def enrich_data():
    """Enrich the raw OHLCV files with IV (30‑day HV) and analyst data.
    The final CSV is written as ``data/raw/{ticker}_180d.csv`` and the
    intermediate ``*_ohlcv.csv`` file is removed.
    """
    import yfinance as yf
    symbols = _read_watchlist()
    for sym in symbols:
        raw_path = f"data/raw/{sym}_ohlcv.csv"
        if not os.path.exists(raw_path):
            print(f"⚠️ 原始文件 {raw_path} 不存在，跳过 {sym} 的补全。")
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
            print(f"⚠️ {sym} 的原始文件缺少收盘价列，跳过 HV 计算。")
            hv = float('nan')
        else:
            hv = _calc_hv(df[close_col])
        # Store IV (whether calculated or NaN)
        df['IV'] = hv
        # Fetch analyst data via yfinance
        try:
            ticker = yf.Ticker(sym)
            info = ticker.info
            target = info.get('targetMeanPrice')
            rating = info.get('recommendationKey')
            df['AnalystTargetPrice'] = target if target is not None else pd.NA
            df['AnalystRating'] = rating if rating is not None else pd.NA
        except Exception as e:
            print(f"⚠️ 获取 {sym} 分析师数据失败: {e}")
            df['AnalystTargetPrice'] = pd.NA
            df['AnalystRating'] = pd.NA
        out_path = f"data/raw/{sym}_180d.csv"
        df.to_csv(out_path, index=False)
        print(f"✅ 已生成完整 CSV: {out_path}")
        # 删除临时文件
        try:
            os.remove(raw_path)
            print(f"🗑 已删除临时文件 {raw_path}")
        except Exception as e:
            print(f"⚠️ 删除临时文件失败: {e}")


def main():
    update_portfolio()
    download_history()
    enrich_data()


if __name__ == '__main__':
    main()
