"""analyze_report.py — 美股投研量化分析引擎

输入依赖：
  - data/raw/{TICKER}_180d.csv     : OHLCV + IV 列，共 180 个交易日
  - data/news/{TICKER}_news.json   : 14 天新闻摘要列表
  - rules.md                       : 持仓规则，包含成本价表格

输出：
  - data/analysis/{TICKER}_metrics.json

核心算法：
  A. Monte Carlo  — 5 000 条路径，估算未来 10 日触及成本价的概率分布
  B. IV Percentile — 当前 IV 在 180 天历史中的百分位（期权贵/便宜判断）
  C. Risk Matrix  — 基于 1.5×ATR / 2×ATR 的动态止损 & 止盈矩阵

风格约定：
  - 全量向量化，pandas / numpy；禁止 Python 级别的逐行循环
  - 类型注解 + docstring
  - 所有中间结果保留在内存，最后一次性写 JSON
"""

from __future__ import annotations

import json
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

# ---------------------------------------------------------------------------
# 1. 数据加载
# ---------------------------------------------------------------------------

def load_price_data(ticker: str) -> pd.DataFrame:
    """读取 180 天 OHLCV + IV CSV，返回按日期升序排列的 DataFrame。

    必须列：Date, open, high, low, close, volume, IV
    """
    path = DATA_DIR / "raw" / f"{ticker}_180d.csv"
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


def load_recent_news(ticker: str, days: int = 14) -> list[dict]:
    """加载最近 ``days`` 天的新闻条目（向量化日期解析）。"""
    path = DATA_DIR / "news" / f"{ticker}_news.json"
    all_news: list[dict] = json.loads(path.read_text(encoding="utf-8"))

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


def extract_cost_price(ticker: str) -> float:
    """从 ``rules.md`` 中提取持仓成本价，若未找到则回退到最新收盘价。

    表格格式示例： ``| TSLA | 20.0 | $390.55 | ...``
    当 ``rules.md`` 中不存在对应 ticker 时，使用该 ticker 最近的收盘价作为成本价，
    这样可以让引擎对任意股票都能运行，而不必手动维护 ``rules.md``。
    """
    rules_path = BASE_DIR / "rules.md"
    text = rules_path.read_text(encoding="utf-8")
    pattern = rf"\|\s*{re.escape(ticker)}\s*\|[^|]*\|\s*\$(?P<price>[0-9,.]+)"
    match = re.search(pattern, text)
    if match:
        return float(match.group("price").replace(",", ""))

    # 回退：读取最新收盘价作为成本价
    price_df = load_price_data(ticker)
    latest_close = float(price_df["close"].iloc[-1])
    return latest_close


# ---------------------------------------------------------------------------
# 2. 核心算法 A — Monte Carlo 价格路径模拟
# ---------------------------------------------------------------------------

def _generate_target_levels(df: pd.DataFrame, cost_price: float) -> dict[str, float]:
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
        rules_path = BASE_DIR / "rules.md"
        text = rules_path.read_text(encoding="utf-8")
        # 捕获第三列之后的任意非管道字符作为备注
        pattern = rf"\|\s*{re.escape(df.name)}\s*\|[^|]*\|\s*\$(?P<price>[0-9,.]+)\s*\|[^|]*\|\s*(?P<note>[^|]+)"
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


def monte_carlo(
    df: pd.DataFrame,
    cost_price: float,
    days: int = 10,
    simulations: int = 5_000,
    seed: int | None = 42,
) -> dict[str, Any]:
    """5 000 条对数正态路径模拟，返回触及成本价的概率分布指标以及自定义目标概率矩阵。

    返回键包括原有的几项，并新增 ``probability_matrix``，其结构为
    ``{target_label: probability_of_final_price≥target}``。
    """
    latest_close: float = df["close"].iloc[-1]
    latest_iv:    float = df["iv"].iloc[-1]

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
    target_levels = _generate_target_levels(df, cost_price)
    probability_matrix: dict[str, float] = {}
    for label, target_price in target_levels.items():
        probability_matrix[label] = float((final_prices >= target_price).mean())

    return {
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
    """计算当前 IV 在 180 天历史窗口的百分位排名。

    百分位 > 50  → 期权相对昂贵（IV 偏高）
    百分位 < 50  → 期权相对便宜（IV 偏低）

    返回键：
      iv_latest       : 最新 IV（小数，如 0.65 表示 65 %）
      iv_percentile   : 0–100 的百分位数
      iv_mean_180d    : 180 天均值
      iv_std_180d     : 180 天标准差
      iv_regime       : "expensive" | "cheap" | "neutral"
    """
    iv_series: pd.Series = df["iv"]
    latest = float(iv_series.iloc[-1])

    # 向量化：比较 latest 与全历史
    pct = float((iv_series <= latest).mean() * 100)

    regime = "expensive" if pct >= 60 else ("cheap" if pct <= 40 else "neutral")

    return {
        "iv_latest":     latest,
        "iv_percentile": round(pct, 2),
        "iv_mean_180d":  round(float(iv_series.mean()), 6),
        "iv_std_180d":   round(float(iv_series.std(ddof=1)), 6),
        "iv_regime":     regime,
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

    def _rr(reward: float, risk: float) -> float:
        return round(reward / risk, 3) if risk > 0 else float("inf")

    rr_1 = _rr(reward, risk_1)
    rr_2 = _rr(reward, risk_2)

    # 评分：基于 1.5×ATR 止损的 RR 比
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


def compute_ma(df: pd.DataFrame, windows: tuple[int, ...] = (5, 20, 60)) -> dict[str, float]:
    """批量计算多周期简单移动平均，返回最新一行。"""
    result: dict[str, float] = {}
    for w in windows:
        result[f"ma_{w}"] = round(float(df["close"].rolling(w, min_periods=1).mean().iloc[-1]), 4)
    return result


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
    cost_price = extract_cost_price(ticker)

    # --- 算法 A: Monte Carlo ---
    mc_metrics = monte_carlo(price_df, cost_price, days=10, simulations=5_000)

    # --- 算法 B: IV Percentile ---
    iv_metrics = iv_percentile(price_df)

    # --- 算法 C: Risk Matrix ---
    rm_metrics = risk_matrix(price_df, cost_price)

    # --- 补充技术指标 ---
    rsi    = compute_rsi(price_df)
    ma_map = compute_ma(price_df, windows=(5, 20, 60))

    # --- 新闻情绪 ---
    sentiment = news_sentiment(news_items)

    # --- 汇总 ---
    output: dict[str, Any] = {
        "meta": {
            "ticker":      ticker,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "cost_price":   cost_price,
            "data_rows":    len(price_df),
        },
        "monte_carlo":   mc_metrics,
        "iv_analysis":   iv_metrics,
        "risk_matrix":   rm_metrics,
        "technicals": {
            "rsi_14": rsi,
            **ma_map,
        },
        "sentiment": sentiment,
    }
    return output


def save_metrics(metrics: dict[str, Any], ticker: str) -> pathlib.Path:
    """将指标字典写入 ``data/analysis/{ticker}_metrics.json``。"""
    out_path = OUT_DIR / f"{ticker}_metrics.json"
    out_path.write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out_path


def main(ticker: str = "TSLA") -> None:
    metrics  = analyze(ticker)
    out_path = save_metrics(metrics, ticker)
    print(f"[OK] 分析完成 → {out_path}")
    # 打印摘要
    mc  = metrics["monte_carlo"]
    iv  = metrics["iv_analysis"]
    rm  = metrics["risk_matrix"]
    print(
        f"  Monte Carlo 始终在成本价上方概率: {mc['prob_always_above']:.1%}\n"
        f"  IV Percentile: {iv['iv_percentile']:.1f}% ({iv['iv_regime']})\n"
        f"  ATR(14): {rm['latest_atr']:.2f}  |  "
        f"止损(1.5×): {rm['stop_1x5_atr']:.2f}  |  "
        f"激进目标: {rm['tp_aggressive']:.2f}  |  "
        f"RR评分: {rm['rr_score']}/10"
    )


if __name__ == "__main__":
    import sys
    _ticker = sys.argv[1] if len(sys.argv) > 1 else "TSLA"
    main(_ticker)