# -*- coding: utf-8 -*-
"""
Download 180‑day historical price, implied volatility (IV) and analyst fundamentals
for the tickers listed in ``data/watchlist.csv``.

The script follows the data‑download conventions defined in ``cline_rules.md``:

* Data source – Interactive Brokers TWS API (``ib_insync``)
* 默认下载天数 – **500** 个日历日（与 ``portfolio_pipeline`` 一致，覆盖 MA200）
* 存储路径 – ``data/raw/{ticker}_180d.csv``

Only the first ticker from the watchlist is processed as a *quick test* (as
requested by the user). The implementation can be easily extended to loop over
all symbols.

The CSV contains the following columns:
``Date,Open,High,Low,Close,Volume,IV,AnalystTargetPrice,AnalystRating``

* ``IV`` – daily implied volatility obtained via ``whatToShow='IVOL'``.
* ``AnalystTargetPrice`` / ``AnalystRating`` – placeholders because the IB
  API does not provide analyst data directly. In a production setting these
  fields would be populated from a dedicated fundamentals service.
"""

import sys
import asyncio
import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OHLCV_CALENDAR_DAYS = 500


def _tws_duration_str(days: int) -> str:
    if days <= 365:
        return f"{days} D"
    years = max(1, (days + 364) // 365)
    return f"{years} Y"


# ---------------------------------------------------------------------------
# Global event‑loop fix for Windows (required by ``ib_insync`` / ``eventkit``)
# ---------------------------------------------------------------------------
if sys.platform.startswith('win'):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        # No explicit loop creation here; ``asyncio.run`` will create its own
        # event loop when needed.
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def check_dependencies():
    """Ensure required third‑party packages are installed.

    On Windows the ``ib_insync`` package (which depends on ``eventkit``) expects
    an event loop to be present at import time. We proactively set the
    ``WindowsSelectorEventLoopPolicy`` before importing the library to avoid the
    ``RuntimeError: There is no current event loop`` that was observed earlier.
    """
    # Windows specific fix for event loop creation before importing ib_insync
    import sys, asyncio
    if sys.platform.startswith('win'):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            # Create and set a default event loop so that eventkit can obtain it
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        except Exception:
            pass

    missing = []
    try:
        import ib_insync  # noqa: F401
    except ImportError:
        missing.append('ib_insync')
    try:
        import pandas  # noqa: F401
    except ImportError:
        missing.append('pandas')
    try:
        import yfinance  # noqa: F401
    except ImportError:
        missing.append('yfinance')
    if missing:
        print(f"错误：缺少依赖库: {', '.join(missing)}")
        print("请运行: pip install " + ' '.join(missing))
        return False
    return True


def read_watchlist(path: str) -> list:
    """Read the CSV watchlist and return a list of ticker symbols.

    The file is a single line of comma‑separated symbols, e.g. ``AAPL,GOOG``.
    Empty entries are ignored.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            line = f.read().strip()
        symbols = [sym.strip() for sym in line.split(',') if sym.strip()]
        return symbols
    except Exception as e:
        print(f"✗ 读取 watchlist 失败: {e}")
        return []


def download_symbol(ib, symbol: str, days: int = OHLCV_CALENDAR_DAYS):
    """Download OHLCV, IV and analyst fundamentals for ``symbol``.

    The function performs three separate IB API calls:
    1. Historical price data (TRADES) for the past ``days``.
    2. Real‑time market data to obtain the latest implied volatility (IV).
    3. Fundamental snapshot to extract the analyst target price.

    A short ``ib.sleep`` is inserted after each request to give the TWS
    server time to push the data back. Returns ``(DataFrame, latest_quote)``
    where ``latest_quote`` is the last price from the IV market-data request
    (for sanity-checking), or returns ``None`` if price history is empty.
    """
    from ib_insync import Stock, util
    import pandas as pd
    import re

    contract = Stock(symbol, 'SMART', 'USD')
    duration = _tws_duration_str(days)
    bar_size = '1 day'

    # ------------------- price data -------------------
    price_bars = ib.reqHistoricalData(
        contract,
        endDateTime='',
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow='TRADES',
        useRTH=True,
        formatDate=1,
    )
    ib.sleep(2)  # ensure data is fully received
    price_df = util.df(price_bars)
    if price_df.empty:
        print(f"✗ {symbol} 没有价格数据")
        return None
    price_df = price_df[['date', 'open', 'high', 'low', 'close', 'volume']]
    price_df = price_df.rename(columns={'date': 'Date'})
    price_df['Date'] = pd.to_datetime(price_df['Date'])
    price_df = price_df.set_index('Date')

    # ------------------- IV data (real‑time) -------------------
    # Request live market data with the generic tick for IV (code 100). This
    # requires a market‑data subscription that includes implied volatility.
    # We use a non‑snapshot request and wait a short period for the data to
    # arrive, then cancel the subscription.
    iv_value = None
    latest_price = None
    try:
        market_data = ib.reqMktData(contract, genericTickList='100', snapshot=False)
        ib.sleep(3)  # give TWS time to push the data
        iv_value = getattr(market_data, 'impliedVolatility', None) or getattr(market_data, 'modelOption', None)
        latest_price = getattr(market_data, 'last', None) or getattr(market_data, 'close', None)
        ib.cancelMktData(market_data)
    except Exception as e:
        print(f"⚠️ IV 数据获取失败: {e}")
    price_df['IV'] = iv_value if iv_value is not None else pd.NA

    # ------------------- Analyst fundamentals -------------------
    # Request a snapshot report (XML) and extract <TargetPrice> if present.
    try:
        fundamentals_xml = ib.reqFundamentalData(contract, reportType='ReportSnapshot')
        ib.sleep(2)
        match = re.search(r'<TargetPrice>([^<]+)</TargetPrice>', fundamentals_xml)
        target_price = float(match.group(1)) if match else pd.NA
    except Exception:
        target_price = pd.NA
    price_df['AnalystTargetPrice'] = target_price
    # Analyst rating is not directly available via the snapshot; leave as NA.
    price_df['AnalystRating'] = pd.NA

    return price_df, latest_price


def main():
    if not check_dependencies():
        sys.exit(1)

    from ib_insync import IB
    import pandas as pd

    # 读取 watchlist（相对项目根目录）
    symbols = read_watchlist(str(ROOT / "data" / "watchlist.csv"))
    if not symbols:
        print('✗ 未在 watchlist 中找到股票代码')
        sys.exit(1)

    # 只取第一个作为快速测试
    test_symbol = symbols[0]
    print(f"⚙️ 仅下载第一个股票作为测试: {test_symbol}（{OHLCV_CALENDAR_DAYS} 日历日）")

    # 连接 TWS（如果不可用则使用本地模拟数据）
    ib = IB()
    use_fallback = False
    try:
        ib.connect('127.0.0.1', 7496, clientId=10)
    except Exception as e:
        print(f"⚠️ 连接 TWS 失败: {e}")
        print("⚠️ 将使用本地生成的模拟数据进行演示。")
        ib = None
        use_fallback = True

    df = None
    latest_quote = None
    if ib:
        try:
            result = download_symbol(ib, test_symbol, days=OHLCV_CALENDAR_DAYS)
            if result is None:
                sys.exit(1)
            df, latest_quote = result
        except Exception as e:
            print(f"✗ 实时下载失败: {e}")
            sys.exit(1)
    else:
        # 使用 yfinance 作为回退数据源，获取历史行情并填充占位列
        try:
            import yfinance as yf
            import pandas as pd
            end_date = datetime.now()
            start_date = end_date - pd.Timedelta(days=OHLCV_CALENDAR_DAYS)
            ticker = yf.Ticker(test_symbol)
            hist = ticker.history(start=start_date, end=end_date, interval='1d')
            if hist.empty:
                raise ValueError('yfinance 返回空数据')
            # 只保留需要的列并重命名
            df = hist[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
            df.index.name = 'Date'
            df.reset_index(inplace=True)
            df['IV'] = pd.NA
            df['AnalystTargetPrice'] = pd.NA
            df['AnalystRating'] = pd.NA
        except Exception as e:
            print(f"✗ 使用 yfinance 作为回退数据源失败: {e}")
            sys.exit(1)

    if df is not None:
        output_path = ROOT / "data" / "raw" / f"{test_symbol}_180d.csv"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path)
        print(f"✓ 数据已保存至 {output_path.as_posix()}")

        last_close = float(df["Close"].iloc[-1])
        print(f"📊 CSV 最后收盘价: {last_close}")
        if latest_quote is not None:
            print(f"⚡ TWS 校验用最新价: {latest_quote}")
            if abs(float(latest_quote) - last_close) < 0.01:
                print("✅ 实时价与 CSV 收盘价基本一致")
            else:
                print("⚠️ 实时价与 CSV 收盘价存在差异（可能非同一时刻或盘前盘后）")
        else:
            print("⚠️ 未能获取 TWS 实时价校验（回退数据源或无行情权限时属正常）")
    else:
        print('✗ 下载过程出现错误')

    if ib is not None:
        try:
            ib.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    main()
