"""analyze_report.py — 美股投研量化分析引擎

输入依赖：
  - data/raw/{TICKER}_180d.csv     : OHLCV + IV（文件名沿用历史；行数可为 500+ 日以支持 MA200）
  - data/news/{TICKER}_news.json   : 14 天新闻摘要列表
  - rules.md                       : 持仓规则，包含成本价表格

输出：
  - data/analysis/{TICKER}_metrics.json

核心算法：
  A. Monte Carlo  — 对数正态（最新 IV），短窗 10 日
  B. IV Percentile — 最近 180 个交易日的 IV 序列分位（滚动 HV 时才有意义）
  C. Risk Matrix  — 1.5×ATR / 2×ATR 止损与止盈参考
  D. Trend        — MA20/60/200 与斜率，输出大趋势 regime
  E. Bootstrap    — 历史对数收益有放回抽样，10/20/60 日路径分位与结构止损触达概率
  F. Market context — SPY（标普 500 ETF）趋势、与标的对齐后的超额收益与收益相关

风格约定：
  - 全量向量化，pandas / numpy；禁止 Python 级别的逐行循环
  - 类型注解 + docstring
  - 所有中间结果保留在内存，最后一次性写 JSON
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 路径配置（可通过环境变量或 CLI 参数覆盖）
# ---------------------------------------------------------------------------
BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
OUT_DIR  = DATA_DIR / "analysis"
OUT_DIR.mkdir(parents=True, exist_ok=True)

IV_PERCENTILE_ROWS = 180
MC_SHORT_DAYS = 10
BOOTSTRAP_HORIZONS = (10, 20, 60)
BOOTSTRAP_SIMS = 5_000
BOOTSTRAP_RETURN_POOL = 252

# 与 ``portfolio_pipeline.BENCHMARK_SYMBOL`` 一致：大盘基准
BENCHMARK_TICKER = "SPY"

# ---------------------------------------------------------------------------
# 1. 数据加载
# ---------------------------------------------------------------------------

def load_price_data(ticker: str) -> pd.DataFrame:
    """读取 OHLCV + IV CSV（通常由流水线写入 ``*_180d.csv``），按日期升序。

    必须列：Date, open, high, low, close, volume, IV
    """
    path = DATA_DIR / "raw" / f"{ticker}_180d.csv"
    if not path.is_file():
        raise FileNotFoundError(f"未找到行情文件: {path}（请先运行数据流水线下载 {ticker}）")
    df = pd.read_csv(path, parse_dates=["Date"])
    df = df.sort_values("Date").reset_index(drop=True)

    # NOTE: 原始 CSV 使用大写列名 ``IV``，但在读取后会统一转为小写。
    # 为避免因大小写不匹配导致的 ``ValueError``，这里将必需列全部使用小写形式。
    required = {"high", "low", "close", "iv"}
    missing = required - set(df.columns.str.lower())
    if missing:
        raise ValueError(f"CSV 缺少必要列: {missing}")

    # 统一小写列名
    df.columns = df.columns.str.lower()
    return df

# ---------------------------------------------------------------------------
# 1.b 额外数据加载：基本面、宏观、替代数据
# ---------------------------------------------------------------------------

def _load_json(path: pathlib.Path) -> dict[str, Any]:
    """安全读取 JSON 文件，若不存在或解析错误返回空字典。"""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

def load_fundamentals(ticker: str) -> dict[str, Any]:
    """加载 data/fundamentals/{ticker}_fundamentals.json"""
    path = DATA_DIR / "fundamentals" / f"{ticker}_fundamentals.json"
    return _load_json(path)

def load_all_fundamentals_pe() -> list[float]:
    """遍历 data/fundamentals/ 下所有文件，收集 PE ratio（跳过缺失或非数值）。"""
    pe_vals: list[float] = []
    fundamentals_dir = DATA_DIR / "fundamentals"
    for file in fundamentals_dir.iterdir():
        if file.suffix != ".json":
            continue
        data = _load_json(file)
        try:
            pe = float(data.get("current_ratios", {}).get("valuation_ratios", {}).get("pe_ratio"))
            if math.isfinite(pe):
                pe_vals.append(pe)
        except Exception:
            continue
    return pe_vals

def load_macro() -> dict[str, Any]:
    """加载宏观数据 data/macroeconomic/macro_data.json"""
    path = DATA_DIR / "macroeconomic" / "macro_data.json"
    return _load_json(path)

def load_alternative(ticker: str) -> dict[str, Any]:
    """加载 data/alternative/{ticker}_alternative.json"""
    path = DATA_DIR / "alternative" / f"{ticker}_alternative.json"
    return _load_json(path)


def load_recent_news(ticker: str, days: int = 14) -> list[dict]:
    """加载最近 ``days`` 天的新闻条目（向量化日期解析）。"""
    path = DATA_DIR / "news" / f"{ticker}_news.json"
    if not path.is_file():
        return []
    try:
        all_news: list[dict] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    # 向量化：将 published / date 字段批量转为 datetime
    raw_dates = pd.Series(
        [item.get("published") or item.get("date") or "" for item in all_news]
    )
    # 规范化 Z 后缀
    raw_dates = raw_dates.str.replace(r"Z$", "+00:00", regex=True)
    parsed = pd.to_datetime(raw_dates, utc=True, errors="coerce")

    mask = parsed >= cutoff
    return [item for item, keep in zip(all_news, mask) if keep]


def extract_cost_price(ticker: str, price_df: pd.DataFrame | None = None) -> float:
    """优先从 `data/holdings/holdings.json` 中读取成本价；找不到时回退到 `rules.md`，再回退到最新收盘价。

    若已在分析流程中加载过行情，传入 ``price_df`` 可避免重复读盘。
    """
    # 1) holdings.json
    holdings_path = BASE_DIR / "data" / "holdings" / "holdings.json"
    try:
        if holdings_path.is_file():
            obj = json.loads(holdings_path.read_text(encoding="utf-8"))
            h = obj.get("holdings", {})
            if ticker in h:
                val = h[ticker].get("averageCost")
                if val is not None:
                    try:
                        return float(val)
                    except Exception:
                        pass
    except Exception:
        # 若解析失败，回退到下一种方式
        pass

    # 2) 兼容旧流程：rules.md 中的成本价
    rules_path = BASE_DIR / "rules.md"
    try:
        text = rules_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    pattern = rf"\|\s*{re.escape(ticker)}\s*\|[^|]*\|\s*\$(?P<price>[0-9,.]+)"
    match = re.search(pattern, text)
    if match:
        try:
            return float(match.group("price").replace(",", ""))
        except Exception:
            pass

    # 3) 最后回退：使用已加载的行情或再读 CSV
    df = price_df if price_df is not None else load_price_data(ticker)
    return float(df["close"].iloc[-1])


# ---------------------------------------------------------------------------
# 2. 核心算法 A — Monte Carlo 价格路径模拟
# ---------------------------------------------------------------------------

def _generate_target_levels(ticker: str, df: pd.DataFrame, cost_price: float) -> dict[str, float]:
    """生成用于概率矩阵的目标价位。

    - 首先尝试从 ``rules.md`` 的 *备注* 列读取自定义目标（如果存在）。
    - 若未找到，则基于最新收盘价生成三个上行和三个下行的百分比目标：
      ``+5%``, ``+10%``, ``+15%`` 以及 ``-5%``, ``-10%``, ``-15%``。
    返回的字典键为标签，值为对应的绝对价格。
    """
    # 当前收盘价
    latest_close: float = float(df["close"].iloc[-1])

    # 读取自定义目标（备注列）
    # 这里假设在 ``rules.md`` 中的持仓表格会有一个可选的备注列，格式类似 ``| TSLA | ... | $390.55 | ... | +5% |``
    # 为简化实现，若未匹配到此类信息则回退到自动生成。
    try:
        # 优先尝试从 holdings.json 的备注字段读取自定义目标
        holdings_path = BASE_DIR / "data" / "holdings" / "holdings.json"
        if holdings_path.is_file():
            obj = json.loads(holdings_path.read_text(encoding="utf-8"))
            h = obj.get("holdings", {})
            if ticker in h:
                note = str(h[ticker].get("note", "") or "").strip()
            else:
                note = ""
        else:
            # 兼容旧流程：从 rules.md 中解析备注列
            rules_path = BASE_DIR / "rules.md"
            text = rules_path.read_text(encoding="utf-8")
            # 持仓表：代码 | 股数 | 成本 | 市价 | 每股盈亏 | 备注（可选百分比目标）
            pattern = (
                rf"\|\s*{re.escape(ticker)}\s*\|[^|]*\|\s*\$(?P<price>[0-9,.]+)\s*\|"
                rf"[^|]*\|[^|]*\|\s*(?P<note>[^|]*)"
            )
            match = re.search(pattern, text)
            if match:
                note = match.group("note").strip()
            # 解析类似 ``+5%,-10%`` 的逗号分隔列表
            targets: dict[str, float] = {}
            for token in re.split(r"[,;]", note):
                token = token.strip()
                if not token:
                    continue
                if token.endswith("%"):
                    try:
                        pct = float(token.rstrip("%")) / 100.0
                        label = f"{token}"
                        targets[label] = latest_close * (1 + pct)
                    except ValueError:
                        continue
            if targets:
                return targets
    except Exception:
        # 读取规则文件或正则匹配失败，安全回退
        pass

    # 自动生成默认目标
    percentages = [5, 10, 15]
    targets: dict[str, float] = {}
    for p in percentages:
        targets[f"+{p}%"] = latest_close * (1 + p / 100.0)
        targets[f"-{p}%"] = latest_close * (1 - p / 100.0)
    return targets


def _latest_iv_for_mc(df: pd.DataFrame) -> float:
    """最新有效 IV；若缺失则用全样本对数收益年化波动率兜底。"""
    raw_cell = df["iv"].iloc[-1]
    if pd.notna(raw_cell):
        raw = float(raw_cell)
        if np.isfinite(raw) and raw > 1e-8:
            return raw
    lr = np.log(df["close"].astype(float) / df["close"].astype(float).shift(1)).dropna()
    if len(lr) < 20:
        return 0.25
    return float(lr.std(ddof=1) * np.sqrt(252))


def monte_carlo(
    df: pd.DataFrame,
    cost_price: float,
    ticker: str,
    days: int = MC_SHORT_DAYS,
    simulations: int = 5_000,
    seed: int | None = 42,
) -> dict[str, Any]:
    """对数正态路径（σ 来自最新 IV），短窗情景；键与历史版本兼容。

    返回键包括原有的几项，并新增 ``probability_matrix``，其结构为
    ``{target_label: probability_of_final_price≥target}``。
    """
    latest_close: float = float(df["close"].iloc[-1])
    latest_iv: float = _latest_iv_for_mc(df)

    sigma_d = latest_iv / np.sqrt(252)
    mu_d    = -0.5 * sigma_d ** 2

    rng = np.random.default_rng(seed)
    log_returns = rng.normal(loc=mu_d, scale=sigma_d, size=(simulations, days))
    price_paths = latest_close * np.exp(np.cumsum(log_returns, axis=1))

    above_cost = price_paths > cost_price
    always_above = above_cost.all(axis=1)
    final_prices = price_paths[:, -1]

    prob_by_day: list[float] = above_cost.mean(axis=0).tolist()

    # 生成目标价位并计算对应的概率矩阵
    target_levels = _generate_target_levels(ticker, df, cost_price)
    probability_matrix: dict[str, float] = {}
    for label, target_price in target_levels.items():
        probability_matrix[label] = float((final_prices >= target_price).mean())

    return {
        "model":                "lognormal_iv",
        "horizon_days":         days,
        "iv_input":             latest_iv,
        "prob_always_above":    float(always_above.mean()),
        "prob_touch_below":     float((~always_above).mean()),
        "expected_final_price": float(final_prices.mean()),
        "pct5_final_price":     float(np.percentile(final_prices, 5)),
        "pct95_final_price":    float(np.percentile(final_prices, 95)),
        "prob_by_day":          prob_by_day,
        "probability_matrix":   probability_matrix,
    }


# ---------------------------------------------------------------------------
# 3. 核心算法 B — IV Percentile（期权维度）
# ---------------------------------------------------------------------------

def iv_percentile(df: pd.DataFrame) -> dict[str, Any]:
    """计算当前 IV 在**传入窗口**（分析侧通常为最近 180 行）的百分位排名。

    百分位 > 50  → 期权相对昂贵（IV 偏高）
    百分位 < 50  → 期权相对便宜（IV 偏低）

    返回键：
      iv_latest       : 最新 IV（小数，如 0.65 表示 65 %）
      iv_percentile   : 0–100 的百分位数
      iv_mean_180d    : 窗口内均值（名称沿用历史）
      iv_std_180d     : 窗口内标准差
      iv_regime       : "expensive" | "cheap" | "neutral"
      window_rows     : 实际用于统计的行数
    """
    valid = df["iv"].dropna()
    if valid.empty:
        return {
            "iv_latest":     float("nan"),
            "iv_percentile": float("nan"),
            "iv_mean_180d":  float("nan"),
            "iv_std_180d":   float("nan"),
            "iv_regime":     "neutral",
            "window_rows":   len(df),
        }
    last_iv = df["iv"].iloc[-1]
    latest = float(last_iv) if pd.notna(last_iv) else float(valid.iloc[-1])
    pct = float((valid <= latest).mean() * 100)

    regime = "expensive" if pct >= 60 else ("cheap" if pct <= 40 else "neutral")

    return {
        "iv_latest":     latest,
        "iv_percentile": round(pct, 2),
        "iv_mean_180d":  round(float(valid.mean()), 6),
        "iv_std_180d":   round(float(valid.std(ddof=1)), 6) if len(valid) > 1 else 0.0,
        "iv_regime":     regime,
        "window_rows":   len(df),
    }


# ---------------------------------------------------------------------------
# 4. 核心算法 C — Risk Matrix（ATR 动态止损/止盈矩阵）
# ---------------------------------------------------------------------------

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """向量化 True Range & ATR（Wilder 平滑移动平均）。"""
    high      = df["high"]
    low       = df["low"]
    prev_close = df["close"].shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder 移动平均：等价于 EWM α = 1/period
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return atr


def risk_matrix(
    df: pd.DataFrame,
    cost_price: float,
) -> dict[str, Any]:
    """基于最新 ATR 输出三档止损 & 止盈价位矩阵。

    止损档位（下行）：
      stop_1x5    : 1.5 × ATR（保命止损，默认）
      stop_2x     : 2.0 × ATR（宽松止损）

    止盈档位（上行）：
      tp_conservative : 0.5 × ATR（保守止盈）
      tp_moderate     : 1.0 × ATR（中性目标）
      tp_aggressive   : 1.5 × ATR（激进目标）

    额外计算：
      risk_reward_1x5   : 激进止盈 / 保命止损 的风险收益比
      risk_reward_2x    : 激进止盈 / 宽松止损 的风险收益比
      rr_score          : 1–10 的综合评分
    """
    atr = compute_atr(df)
    latest_close = float(df["close"].iloc[-1])
    latest_atr   = float(atr.iloc[-1])

    stop_1x5 = latest_close - 1.5 * latest_atr
    stop_2x  = latest_close - 2.0 * latest_atr

    tp_conservative = latest_close + 0.5 * latest_atr
    tp_moderate     = latest_close + 1.0 * latest_atr
    tp_aggressive   = latest_close + 1.5 * latest_atr

    # 基于成本价的风险（cost_price → stop）以防止持仓成本高于当前价的情况
    reward = tp_aggressive - cost_price
    risk_1 = cost_price - stop_1x5
    risk_2 = cost_price - stop_2x

    def _rr(reward: float, risk: float) -> float | None:
        if risk > 0:
            return round(reward / risk, 3)
        return None

    rr_1 = _rr(reward, risk_1)
    rr_2 = _rr(reward, risk_2)

    if risk_1 <= 0:
        rr_note = "structural_stop_above_cost; rr_vs_cost_not_applicable"
        rr_score = 10
    elif rr_1 is None:
        rr_note = "n/a"
        rr_score = 5
    else:
        rr_note = "risk_measured_as_cost_minus_stop_1x5"
        rr_score = (
            10 if rr_1 >= 3
            else 8 if rr_1 >= 2
            else 6 if rr_1 >= 1.5
            else 4 if rr_1 >= 1
            else 2
        )

    return {
        "latest_close":     round(latest_close, 4),
        "latest_atr":       round(latest_atr, 4),
        "stop_1x5_atr":     round(stop_1x5, 4),
        "stop_2x_atr":      round(stop_2x, 4),
        "tp_conservative":  round(tp_conservative, 4),
        "tp_moderate":      round(tp_moderate, 4),
        "tp_aggressive":    round(tp_aggressive, 4),
        "risk_reward_1x5":  rr_1,
        "risk_reward_2x":   rr_2,
        "rr_score":         rr_score,
        "rr_note":          rr_note,
    }


# ---------------------------------------------------------------------------
# 5. 补充指标 — RSI、MA、新闻情绪
# ---------------------------------------------------------------------------

def compute_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """Wilder RSI，返回最新值。"""
    delta = df["close"].diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def compute_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, float]:
    """MACD指标：快线、慢线、MACD线、信号线、柱状图"""
    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    
    return {
        "macd_line": round(float(macd_line.iloc[-1]), 4),
        "signal_line": round(float(signal_line.iloc[-1]), 4),
        "histogram": round(float(histogram.iloc[-1]), 4),
        "momentum": "bullish" if histogram.iloc[-1] > 0 else "bearish"
    }


def compute_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> dict[str, float]:
    """布林带：上轨、中轨、下轨，当前价位置"""
    close = df["close"]
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = sma + (std * std_dev)
    lower = sma - (std * std_dev)
    
    current = float(close.iloc[-1])
    upper_val = float(upper.iloc[-1])
    lower_val = float(lower.iloc[-1])
    
    position = "above_upper" if current > upper_val else "below_lower" if current < lower_val else "within_bands"
    
    return {
        "upper_band": round(upper_val, 4),
        "middle_band": round(float(sma.iloc[-1]), 4),
        "lower_band": round(lower_val, 4),
        "bandwidth": round((upper_val - lower_val) / float(sma.iloc[-1]), 4),
        "position": position
    }


def fetch_vix_data() -> dict[str, float]:
    """获取当前VIX指数值（恐慌指数）"""
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1d")
        if not hist.empty:
            latest_vix = float(hist['Close'].iloc[-1])
            return {
                "vix_current": round(latest_vix, 2),
                "fear_level": "extreme_fear" if latest_vix > 30 else "fear" if latest_vix > 20 else "neutral" if latest_vix > 15 else "greed"
            }
        else:
            return {"vix_current": None, "fear_level": "unknown"}
    except Exception as e:
        return {"vix_current": None, "fear_level": "error"}


def compute_ma(df: pd.DataFrame, windows: tuple[int, ...] = (5, 20, 60, 200)) -> dict[str, float | None]:
    """多周期 SMA；``min_periods`` 与窗口相同，故 MA200 在样本不足时为 ``None``。"""
    close = df["close"].astype(float)
    result: dict[str, float | None] = {}
    for w in windows:
        ser = close.rolling(w, min_periods=w).mean()
        v = ser.iloc[-1]
        result[f"ma_{w}"] = round(float(v), 4) if pd.notna(v) else None
    return result


def trend_regime(df: pd.DataFrame) -> dict[str, Any]:
    """基于 MA20/60/200 与短期斜率的大趋势标签（供加减仓与止损语境使用）。"""
    close = df["close"].astype(float)
    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()
    ma200 = close.rolling(200, min_periods=200).mean()

    c = float(close.iloc[-1])
    m20 = ma20.iloc[-1]
    m60 = ma60.iloc[-1]
    m200 = ma200.iloc[-1]

    slope_ma20: float | None = None
    if len(ma20.dropna()) > 6 and pd.notna(ma20.iloc[-6]) and float(ma20.iloc[-6]) != 0:
        slope_ma20 = round((float(ma20.iloc[-1]) - float(ma20.iloc[-6])) / abs(float(ma20.iloc[-6])), 6)

    slope_ma60: float | None = None
    if len(ma60.dropna()) > 6 and pd.notna(ma60.iloc[-6]) and float(ma60.iloc[-6]) != 0:
        slope_ma60 = round((float(ma60.iloc[-1]) - float(ma60.iloc[-6])) / abs(float(ma60.iloc[-6])), 6)

    ma200_ok = pd.notna(m200)
    bull_core = (
        ma200_ok
        and pd.notna(m20)
        and pd.notna(m60)
        and c > float(m200)
        and float(m20) > float(m60) > float(m200)
    )
    bear_core = ma200_ok and pd.notna(m20) and pd.notna(m60) and c < float(m200) and float(m20) < float(m60)
    bull_partial = (not bull_core) and pd.notna(m60) and c > float(m60) and pd.notna(m20) and float(m20) > float(m60)

    if bull_core:
        regime = "bull_core"
        hint = "多头排列+价在 MA200 上方：大趋势偏多；减仓宜分批止盈，加仓宜等回撤至均线带。"
    elif bear_core:
        regime = "bear_core"
        hint = "价在 MA200 下方且短中期均线空头：大趋势偏空；加仓需谨慎，止损纪律优先。"
    elif bull_partial:
        regime = "bull_partial"
        hint = "中期仍强但未确认年线多头；可等待突破/回踩 MA200 再加大仓位。"
    else:
        regime = "neutral"
        hint = "趋势不明或震荡；宜控制仓位，以结构止损与情景概率为主。"

    return {
        "regime":            regime,
        "close_vs_ma200":    round(c - float(m200), 4) if ma200_ok else None,
        "ma_spread_20_60":   round(float(m20) - float(m60), 4) if pd.notna(m20) and pd.notna(m60) else None,
        "slope_ma20_5d":     slope_ma20,
        "slope_ma60_5d":     slope_ma60,
        "ma200_available":   ma200_ok,
        "positioning_hint":  hint,
    }


def load_benchmark_data() -> pd.DataFrame | None:
    """读取 ``data/raw/SPY_180d.csv``；缺失时返回 ``None``（不中断单标的分析）。"""
    path = DATA_DIR / "raw" / f"{BENCHMARK_TICKER}_180d.csv"
    if not path.is_file():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["Date"])
        df = df.sort_values("Date").reset_index(drop=True)
        req = {"high", "low", "close", "iv"}
        if req - set(df.columns.str.lower()):
            return None
        df.columns = df.columns.str.lower()
        return df
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return None


def compute_market_context(
    stock_df: pd.DataFrame,
    spy_df: pd.DataFrame | None,
    stock_trend_regime: str,
) -> dict[str, Any]:
    """标普基准：SPY 自身趋势 + 与标的 **同日对齐** 的超额与相关（全向量化）。"""
    if spy_df is None or spy_df.empty:
        return {
            "benchmark_ticker": BENCHMARK_TICKER,
            "available":        False,
            "note":             "缺少 data/raw/SPY_180d.csv，请运行 scripts/portfolio_pipeline.py 下载基准",
        }

    spy_trend = trend_regime(spy_df)
    spy_ma    = compute_ma(spy_df, windows=(20, 60, 200))

    left = stock_df[["date", "close"]].copy()
    right = spy_df[["date", "close"]].rename(columns={"close": "spy_close"})
    m = pd.merge(left, right, on="date", how="inner").sort_values("date").reset_index(drop=True)

    def _window_total_return(series: pd.Series, n: int) -> float | None:
        if len(series) <= n or series.iloc[-1 - n] == 0:
            return None
        return float(series.iloc[-1] / series.iloc[-1 - n] - 1.0)

    n20, n60 = 20, 60
    r_s20 = _window_total_return(m["close"], n20)
    r_b20 = _window_total_return(m["spy_close"], n20)
    r_s60 = _window_total_return(m["close"], n60)
    r_b60 = _window_total_return(m["spy_close"], n60)

    ex20 = (r_s20 - r_b20) if (r_s20 is not None and r_b20 is not None) else None
    ex60 = (r_s60 - r_b60) if (r_s60 is not None and r_b60 is not None) else None

    lr_s = np.log(m["close"].astype(float) / m["close"].astype(float).shift(1))
    lr_b = np.log(m["spy_close"].astype(float) / m["spy_close"].astype(float).shift(1))
    pair = pd.DataFrame({"ls": lr_s, "lb": lr_b}).dropna()
    tail = pair.tail(60)
    corr_60 = float(tail["ls"].corr(tail["lb"])) if len(tail) >= 20 else None

    notes: list[str] = []
    sr, br = spy_trend.get("regime"), stock_trend_regime
    if br in ("bull_core", "bull_partial") and sr in ("bear_core", "neutral"):
        notes.append("个股趋势偏强而大盘偏弱或中性：关注是否为独立基本面或短期相对强度。")
    if br in ("bear_core", "neutral") and sr in ("bull_core", "bull_partial"):
        notes.append("大盘偏强而标的偏弱：回撤可能更多来自个股/板块而非系统性恐慌。")
    if ex60 is not None and ex60 > 0.05:
        notes.append("近60日显著跑赢SPY，注意获利了结与波动放大。")
    if ex60 is not None and ex60 < -0.05:
        notes.append("近60日显著跑输SPY，结合成本与止损概率审视仓位。")
    if not notes:
        notes.append("大盘与个股趋势组合未见极端分化；仍以标的自身 bootstrap 与 ATR 为主。")

    return {
        "benchmark_ticker":     BENCHMARK_TICKER,
        "available":            True,
        "aligned_trading_days": len(m),
        "spy_trend_regime":     spy_trend.get("regime"),
        "spy_trend":            spy_trend,
        "spy_ma":               spy_ma,
        "stock_total_return_20d": round(r_s20, 6) if r_s20 is not None else None,
        "spy_total_return_20d":   round(r_b20, 6) if r_b20 is not None else None,
        "excess_return_20d":      round(ex20, 6) if ex20 is not None else None,
        "stock_total_return_60d": round(r_s60, 6) if r_s60 is not None else None,
        "spy_total_return_60d":   round(r_b60, 6) if r_b60 is not None else None,
        "excess_return_60d":      round(ex60, 6) if ex60 is not None else None,
        "log_return_corr_60d":    round(corr_60, 4) if corr_60 is not None else None,
        "benchmark_notes":       notes,
    }


def bootstrap_path_metrics(
    df: pd.DataFrame,
    cost_price: float,
    ticker: str,
    risk: dict[str, Any],
    horizons: tuple[int, ...] = BOOTSTRAP_HORIZONS,
    simulations: int = BOOTSTRAP_SIMS,
    seed: int = 42,
    return_pool_max: int = BOOTSTRAP_RETURN_POOL,
) -> dict[str, Any]:
    """历史对数收益 i.i.d. bootstrap，输出多 horizon 终点分位与结构位触达概率。"""
    close = df["close"].astype(float)
    s0 = float(close.iloc[-1])
    log_r = np.log(close / close.shift(1)).dropna().to_numpy(dtype=float)
    if len(log_r) < 40:
        return {
            "method": "historical_log_return_bootstrap",
            "error":  "insufficient_log_returns",
            "rows":   len(log_r),
        }

    pool = log_r[-return_pool_max:] if len(log_r) > return_pool_max else log_r
    max_h = max(horizons)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(pool), size=(simulations, max_h))
    draws = pool[idx]
    paths = s0 * np.exp(np.cumsum(draws, axis=1))

    ma200_ser = close.rolling(200, min_periods=200).mean()
    ma200_lvl = float(ma200_ser.iloc[-1]) if pd.notna(ma200_ser.iloc[-1]) else None

    stop_1x5 = float(risk["stop_1x5_atr"])
    stop_2x = float(risk["stop_2x_atr"])
    tp_aggr = float(risk["tp_aggressive"])

    target_levels = _generate_target_levels(ticker, df, cost_price)
    per_h: dict[str, Any] = {}

    for h in horizons:
        sub = paths[:, :h]
        mins = sub.min(axis=1)
        maxes = sub.max(axis=1)
        finals = sub[:, h - 1]
        always_above_cost = (sub > cost_price).all(axis=1)

        block: dict[str, Any] = {
            "final_median":                      float(np.median(finals)),
            "final_pct5":                        float(np.percentile(finals, 5)),
            "final_pct25":                       float(np.percentile(finals, 25)),
            "final_pct75":                       float(np.percentile(finals, 75)),
            "final_pct95":                       float(np.percentile(finals, 95)),
            "min_price_mae_from_start_pct5":   float(np.percentile((mins - s0) / s0 * 100.0, 5)),
            "min_price_mae_from_start_median": float(np.median((mins - s0) / s0 * 100.0)),
            "max_price_runup_from_start_pct95": float(np.percentile((maxes - s0) / s0 * 100.0, 95)),
            "prob_touch_or_below_stop_1x5":     float((mins <= stop_1x5).mean()),
            "prob_touch_or_below_stop_2x":      float((mins <= stop_2x).mean()),
            "prob_touch_or_below_cost":         float((mins <= cost_price).mean()),
            "prob_final_at_or_above_tp_aggressive": float((finals >= tp_aggr).mean()),
            "prob_paths_always_above_cost":     float(always_above_cost.mean()),
        }
        if ma200_lvl is not None:
            block["prob_touch_or_below_ma200"] = float((mins <= ma200_lvl).mean())

        pm: dict[str, float] = {}
        for label, lvl in target_levels.items():
            pm[label] = float((finals >= lvl).mean())
        block["probability_matrix_final"] = pm
        per_h[str(h)] = block

    return {
        "method":             "historical_log_return_bootstrap",
        "simulations":      simulations,
        "return_pool_len":  len(pool),
        "s0":               s0,
        "ma200_level":      ma200_lvl,
        "horizons":         per_h,
    }


_NEG_KW = ["risk", "concern", "challenge", "uncertainty", "downturn", "loss", "negative",
           "warning", "decline", "cut", "lawsuit", "recall", "miss"]
_POS_KW = ["growth", "opportunity", "beat", "upgrade", "positive", "gain", "record",
           "expansion", "partnership", "breakthrough", "raised", "target", "bullish"]


def news_sentiment(news_items: list[dict]) -> dict[str, Any]:
    """向量化新闻情绪分析（使用 pandas str 方法批量统计关键词）。

    返回键：
      total_articles      : 最近 14 天文章数
      net_sentiment_score : 正面关键词总数 − 负面关键词总数
      top_bearish_title   : 负面得分最高的文章标题
      top_bullish_title   : 正面得分最高的文章标题
      keyword_freq        : dict，常用关键词出现次数（AI, Delivery, Earnings）
    """
    if not news_items:
        return {
            "total_articles":      0,
            "net_sentiment_score": 0,
            "top_bearish_title":   None,
            "top_bullish_title":   None,
            "keyword_freq":        {},
        }

    df_news = pd.DataFrame(news_items)
    # 合并 title + summary 为统一文本列（小写）
    texts: pd.Series = (
        df_news.get("title", pd.Series([""] * len(df_news))).fillna("") + " " +
        df_news.get("summary", pd.Series([""] * len(df_news))).fillna("")
    ).str.lower()

    # 向量化：每行对每个关键词计数 → sum across keywords
    neg_counts = sum(texts.str.count(kw) for kw in _NEG_KW)
    pos_counts = sum(texts.str.count(kw) for kw in _POS_KW)

    net_score = int(pos_counts.sum() - neg_counts.sum())

    titles = df_news.get("title", pd.Series([""] * len(df_news))).fillna("").tolist()
    top_bearish = titles[int(neg_counts.argmax())] if len(titles) else None
    top_bullish = titles[int(pos_counts.argmax())] if len(titles) else None

    # 特定关键词频次
    track_kws = ["ai", "delivery", "earnings", "recall", "fsd", "cybertruck", "margin"]
    kw_freq = {kw: int(texts.str.count(kw).sum()) for kw in track_kws}

    return {
        "total_articles":      len(news_items),
        "net_sentiment_score": net_score,
        "top_bearish_title":   top_bearish,
        "top_bullish_title":   top_bullish,
        "keyword_freq":        kw_freq,
    }


# ---------------------------------------------------------------------------
# 6. 主流程 — 汇总所有指标并输出 JSON
# ---------------------------------------------------------------------------

def analyze(ticker: str = "TSLA") -> dict[str, Any]:
    """执行全量分析，返回汇总指标字典。"""
    # --- 加载 ---
    price_df   = load_price_data(ticker)
    news_items = load_recent_news(ticker, days=14)
    cost_price = extract_cost_price(ticker, price_df=price_df)

    iv_window = price_df.tail(min(IV_PERCENTILE_ROWS, len(price_df)))

    # --- 算法 A: Monte Carlo（短窗 IV 对数正态）---
    mc_metrics = monte_carlo(price_df, cost_price, ticker, days=MC_SHORT_DAYS, simulations=5_000)

    # --- 算法 B: IV Percentile（最近 180 行，滚动 HV 下有意义）---
    iv_metrics = iv_percentile(iv_window)

    # --- 算法 C: Risk Matrix ---
    rm_metrics = risk_matrix(price_df, cost_price)

    # --- 大趋势 + 均线 + 新技术指标 ---
    trend = trend_regime(price_df)
    rsi   = compute_rsi(price_df)
    ma_map = compute_ma(price_df, windows=(5, 20, 60, 200))
    macd = compute_macd(price_df)
    bb = compute_bollinger_bands(price_df)
    vix = fetch_vix_data()

    spy_df = load_benchmark_data()
    market_ctx = compute_market_context(price_df, spy_df, trend["regime"])

    # --- 历史 bootstrap 情景（止盈止损触达概率）---
    boot_metrics = bootstrap_path_metrics(
        price_df, cost_price, ticker, rm_metrics,
        horizons=BOOTSTRAP_HORIZONS,
        simulations=BOOTSTRAP_SIMS,
    )

    # --- 新闻情绪 ---
    sentiment = news_sentiment(news_items)

    # ---------------------------------------------------------------------
    # 7. 额外维度计算：PE 百分位、10Y 国债相关性、情绪‑价格背离度
    # ---------------------------------------------------------------------
    # 7.1 PE 百分位（相对于所有已收集的基本面数据）
    fundamentals = load_fundamentals(ticker)
    current_pe = fundamentals.get("current_ratios", {}).get("valuation_ratios", {}).get("pe_ratio")
    pe_percentile: float | None = None
    if isinstance(current_pe, (int, float)) and not math.isnan(current_pe):
        all_pe = load_all_fundamentals_pe()
        if all_pe:
            # 计算当前 PE 在所有 PE 中的百分位（<= 当前值的比例）
            pe_percentile = 100.0 * sum(1 for p in all_pe if p <= current_pe) / len(all_pe)

    # 7.2 与 10 年期国债收益率的相关性（使用对数收益率）
    treasury_corr: float | None = None
    macro = load_macro()
    ten_year = (
        macro.get("indicators", {})
        .get("ten_year_treasury", {})
        .get("series", {})
    )
    if ten_year and not price_df.empty:
        # 构建 pandas Series，索引为日期
        treasury_series = pd.Series(ten_year, dtype=float)
        treasury_series.index = pd.to_datetime(treasury_series.index)
        # 对齐到股票交易日，向前填充缺失值
        treasury_aligned = treasury_series.reindex(price_df["date"], method="ffill")
        # 计算对数收益率（若出现 0 或负值则使用 pct_change）
        stock_lr = np.log(price_df["close"].astype(float) / price_df["close"].astype(float).shift(1)).dropna()
        treasury_lr = np.log(treasury_aligned.astype(float) / treasury_aligned.astype(float).shift(1)).dropna()
        # 取交集
        common = stock_lr.index.intersection(treasury_lr.index)
        if len(common) > 1:
            treasury_corr = float(stock_lr.loc[common].corr(treasury_lr.loc[common]))

    # 7.3 情绪‑价格背离度：情绪净分数 与 最近 14 天价格涨跌幅的差异
    sentiment_divergence: float | None = None
    net_sent = sentiment.get("net_sentiment_score")
    if isinstance(net_sent, (int, float)) and len(price_df) >= 14:
        price_change = float(price_df["close"].iloc[-1] / price_df["close"].iloc[-14] - 1.0)
        # 将价格涨跌幅转为百分比后比较
        sentiment_divergence = net_sent - (price_change * 100.0)

    extra_dimensions = {
        "pe_percentile": round(pe_percentile, 2) if pe_percentile is not None else None,
        "ten_year_treasury_corr": round(treasury_corr, 4) if treasury_corr is not None else None,
        "sentiment_price_divergence": round(sentiment_divergence, 2) if sentiment_divergence is not None else None,
    }

    dcol = price_df["date"] if "date" in price_df.columns else None
    if dcol is not None:
        h_first = str(pd.to_datetime(dcol.iloc[0]).date())
        h_last = str(pd.to_datetime(dcol.iloc[-1]).date())
    else:
        h_first, h_last = None, None

    # --- 汇总 ---
    output: dict[str, Any] = {
        "meta": {
            "ticker":      ticker,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "cost_price":   cost_price,
            "data_rows":    len(price_df),
            "history_first_date": h_first,
            "history_last_date":  h_last,
            "iv_percentile_window_rows": len(iv_window),
            "bootstrap_horizons":        list(BOOTSTRAP_HORIZONS),
            "benchmark_ticker":          BENCHMARK_TICKER,
        },
        "trend":          trend,
        "market_context": market_ctx,
        "monte_carlo":   mc_metrics,
        "bootstrap":     boot_metrics,
        "iv_analysis":   iv_metrics,
        "risk_matrix":   rm_metrics,
        "technicals": {
            "rsi_14": rsi,
            **ma_map,
            **macd,
            **bb,
            **vix,
        },
        "sentiment": sentiment,
        "extra_dimensions": extra_dimensions,
    }
    return output


def _read_watchlist_symbols() -> list[str]:
    """读取 ``data/watchlist.csv``（单行逗号分隔）。"""
    path = DATA_DIR / "watchlist.csv"
    if not path.is_file():
        return []
    line = path.read_text(encoding="utf-8").strip()
    return [s.strip().upper() for s in line.split(",") if s.strip()]


def _sanitize_for_json(obj: Any) -> Any:
    """将 ``nan`` / ``inf`` 转为 ``null``，保证 JSON 严格可解析。"""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def save_metrics(metrics: dict[str, Any], ticker: str) -> pathlib.Path:
    """将指标字典写入 ``data/analysis/{ticker}_metrics.json``。"""
    out_path = OUT_DIR / f"{ticker}_metrics.json"
    safe = _sanitize_for_json(metrics)
    out_path.write_text(
        json.dumps(safe, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out_path


def _print_summary(metrics: dict[str, Any], ticker: str) -> None:
    mc = metrics["monte_carlo"]
    iv = metrics["iv_analysis"]
    rm = metrics["risk_matrix"]
    tr = metrics["trend"]
    boot = metrics.get("bootstrap") or {}
    ivp = iv.get("iv_percentile")
    iv_str = f"{ivp:.1f}%" if isinstance(ivp, (int, float)) and not (isinstance(ivp, float) and math.isnan(ivp)) else "n/a"

    b20 = (boot.get("horizons") or {}).get("20") if isinstance(boot, dict) else None
    b_line = ""
    if isinstance(b20, dict) and "prob_touch_or_below_stop_1x5" in b20:
        b_line = (
            f"\n  Bootstrap 20d 触及 1.5×ATR 止损参考概率: "
            f"{b20['prob_touch_or_below_stop_1x5']:.1%}"
        )

    mk = metrics.get("market_context") or {}
    mk_line = ""
    if mk.get("available"):
        ex = mk.get("excess_return_20d")
        exs = f"{ex:+.2%}" if isinstance(ex, (int, float)) and not math.isnan(ex) else "n/a"
        mk_line = f"\n  SPY趋势: {mk.get('spy_trend_regime')}  |  20日超额(vs SPY): {exs}  |  60日相关: {mk.get('log_return_corr_60d')}"
    else:
        mk_line = f"\n  SPY基准: 未加载 ({mk.get('note', '')})"

    print(
        f"[{ticker}] 趋势: {tr['regime']}  |  "
        f"MonteCarlo(10d) 始终高于成本: {mc['prob_always_above']:.1%}\n"
        f"  IV Percentile({iv.get('window_rows', '?')}行窗口): {iv_str} ({iv['iv_regime']})\n"
        f"  ATR(14): {rm['latest_atr']:.2f}  |  "
        f"止损(1.5×): {rm['stop_1x5_atr']:.2f}  |  "
        f"激进目标: {rm['tp_aggressive']:.2f}  |  "
        f"RR评分: {rm['rr_score']}/10"
        f"{b_line}"
        f"{mk_line}"
    )


def main(ticker: str = "TSLA") -> None:
    """程序化入口：分析单个标的并打印摘要。"""
    metrics = analyze(ticker)
    out_path = save_metrics(metrics, ticker)
    print(f"[OK] 分析完成 → {out_path}")
    _print_summary(metrics, ticker)
    # ---------------------------------------------------------------------
    # 自动入库钩子：将本次分析结果、基本面和宏观数据写入 SQLite
    # ---------------------------------------------------------------------
    try:
        # 动态加载内部帮助模块（避免相对导入在 __main__ 执行时失效）
        import importlib.util
        helper_path = BASE_DIR / "scripts" / "_db_ingest_helper.py"
        spec = importlib.util.spec_from_file_location("_db_ingest_helper", helper_path)
        helper = importlib.util.module_from_spec(spec)
        assert spec and spec.loader  # 类型检查安全
        spec.loader.exec_module(helper)
        helper.ingest_all(ticker, out_path)
        print(f"[OK] 数据已入库 (quantitative, fundamentals, macro)")
    except Exception as exc:  # pragma: no cover
        print(f"[WARN] 入库失败: {exc}")


def _cli_main() -> None:
    parser = argparse.ArgumentParser(description="美股投研量化分析 → data/analysis/{TICKER}_metrics.json")
    parser.add_argument("tickers", nargs="*", help="股票代码（可多个），省略时默认为 TSLA")
    parser.add_argument("--all", action="store_true", help="分析 data/watchlist.csv 中的全部标的")
    args = parser.parse_args()

    if args.all:
        symbols = _read_watchlist_symbols()
        if not symbols:
            print("[WARN] 未找到 watchlist 或列表为空（data/watchlist.csv）")
            return
    elif args.tickers:
        symbols = [t.upper() for t in args.tickers]
    else:
        symbols = ["TSLA"]

    had_error = False
    for sym in symbols:
        try:
            metrics = analyze(sym)
            out_path = save_metrics(metrics, sym)
            print(f"[OK] {sym} → {out_path}")
            _print_summary(metrics, sym)
        except Exception as exc:  # noqa: BLE001 — CLI 需汇总错误并继续
            had_error = True
            print(f"[ERR] [{sym}] {exc}")

    if had_error:
        raise SystemExit(1)


if __name__ == "__main__":
    _cli_main()