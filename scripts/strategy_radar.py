"""strategy_radar.py — 高胜率量化监控哨所

五模块策略系统：
  模块一：宏观熔断器与标的筛选（Macro & Asset Filter）
  模块二：Reverse_Core_Buy_Protocol（左侧黄金建仓）
  模块三：右侧加仓与风险追踪（Right-side Addition & Trailing）
  模块四：防洗飞与右侧回补协议（Anti-Washout Protocol）
  模块五：多维阶梯式出场与动态资金管理协议（Multi-Stage Exit & Capital Allocation）

数据库：investment_lab.db（使用 ticker_metrics + quantitative 表）
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import pandas as pd
import numpy as np

# ============================================================================
# 配置常量
# ============================================================================

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "investment_lab.db"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 持仓记录文件（手动维护）
HOLDINGS_CSV = BASE_DIR / "data" / "holdings.csv"

# 模块一：大盘熔断状态
MACRO_BULL_STATES = frozenset({"bull_core", "neutral_bull"})

# 模块二：左侧建仓参数
LEFT_ENTRY_POSITION_MIN = 0.10
LEFT_ENTRY_POSITION_MAX = 0.15
LEFT_MATRIX_WINDOW_DAYS = 3

PE_PERCENTILE_THRESHOLD = 15.0      # PE_Percentile ≤ 15%（90日滚动价格分位）
PEG_THRESHOLD = 1.2                 # PEG ≤ 1.2（高成长标的估值通道）
RSI_THRESHOLD = 40.0                # RSI ≤ 40（回测二次校准：捕捉熊市超卖窗口）
CROWDING_THRESHOLD = 5.0            # Crowding ≤ 5.0（硬编码严苛标准）
PERCENTILE_LOW = 15.0               # 历史低位分位数（15%），用于 RSI/Crowding 动态阈值
WIN_PROB_FLIP_THRESHOLD = 5.0       # Win_Prob_10d 扭转阈值 (%)
WIN_PROB_MIN_SPIKE = 15.0           # Win_Prob_10d 回升脉冲阈值（当日-前日≥此值即触发）

LEFT_ATR_MULTIPLIER = 2.0           # 左侧硬止损 2.0×ATR

# 模块三：右侧加仓参数
RIGHT_ATR_MULTIPLIER = 1.5          # 右侧收紧后 1.5×ATR

# 模块四：防洗飞参数
WASHOUT_LOOKBACK_N = 5              # 前N个交易日内触发止损
WASHOUT_ALPHA_HIGH_VOL = 0.015      # 高波动标的 α
WASHOUT_ALPHA_STANDARD = 0.010      # 标准标的 α
WASHOUT_HIGH_VOL_THRESHOLD = 0.04   # ATR/Price > 4% 视为高波动
WASHOUT_CONSECUTIVE_DAYS = 2        # 连续站上阈值天数
WASHOUT_VOLUME_SURGE = 1.3          # 放量突破倍数
WASHOUT_RSI_REBOUND = 40.0          # RSI 突破 40 动能线

# ============================================================================
# 工具函数
# ============================================================================


def _read_holdings_csv() -> dict[str, list[dict[str, Any]]]:
    """读取 data/holdings.csv 获取持仓与交易记录。

    CSV 列：Date, Ticker, Buy_Price, Buy_Qty, Sell_Price, Sell_Qty

    如果某个 ticker 的买入记录存在但卖出为空/0，表示仍在持有。
    返回 {TICKER: [trade_records]}，按日期升序排列。
    """
    result: dict[str, list[dict[str, Any]]] = {}
    if not HOLDINGS_CSV.is_file():
        return result
    try:
        df = pd.read_csv(HOLDINGS_CSV)
        df.columns = df.columns.str.strip()
        required = {"Date", "Ticker"}
        if not required.issubset(set(df.columns)):
            return result
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values("Date").reset_index(drop=True)
        for _, row in df.iterrows():
            t = str(row["Ticker"]).strip().upper()
            if not t or t == "NAN":
                continue
            rec = {
                "date": str(row["Date"])[:10] if pd.notna(row["Date"]) else "",
                "ticker": t,
                "buy_price": float(row.get("Buy_Price", 0) or 0),
                "buy_qty": float(row.get("Buy_Qty", 0) or 0),
                "sell_price": float(row.get("Sell_Price", 0) or 0),
                "sell_qty": float(row.get("Sell_Qty", 0) or 0),
            }
            result.setdefault(t, []).append(rec)
    except Exception:
        pass
    return result


def _get_active_holdings(records: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """从交易记录中提取当前仍在持有的标的（买入但未卖出）。

    返回 {ticker: {"avg_cost": float, "total_qty": float, "last_buy_date": str}}
    """
    active: dict[str, dict[str, Any]] = {}
    for ticker, trades in records.items():
        # 跳过 CASH 行（由 _enrich_holdings_with_market_data 单独处理）
        if ticker == "CASH":
            continue
        total_bought = 0.0
        total_sold = 0.0
        cost_sum = 0.0
        last_buy_date = ""
        first_buy_found = False
        for t in trades:
            if t["buy_qty"] > 0:
                first_buy_found = True
                total_bought += t["buy_qty"]
                cost_sum += t["buy_price"] * t["buy_qty"]
                last_buy_date = t["date"]
            if t["sell_qty"] > 0:
                if first_buy_found:
                    total_sold += t["sell_qty"]
        net = total_bought - total_sold
        if net > 0.001 and total_bought > 0:
            active[ticker] = {
                "avg_cost": cost_sum / total_bought if total_bought > 0 else 0,
                "total_qty": net,
                "last_buy_date": last_buy_date,
            }
    return active


def _get_last_stop_out_30d(records: dict[str, list[dict[str, Any]]],
                           ticker: str,
                           latest_date: str = "") -> dict[str, Any] | None:
    """自动回溯 holdings.csv 过去 30 天内该 ticker 的止损卖出记录。

    返回 {"date": str, "exit_price": float, "days_ago": int} 或 None。

    只有卖出发生在 30 天窗口内的记录才会被返回；超过 30 天的忽略。
    """
    if ticker not in records:
        return None
    sells = [t for t in records[ticker] if t["sell_qty"] > 0 and t["sell_price"] > 0]
    if not sells:
        return None
    try:
        ref_dt = datetime.strptime(latest_date[:10], "%Y-%m-%d") if latest_date else datetime.now()
    except Exception:
        ref_dt = datetime.now()
    # 筛选 30 天内的卖出
    recent = []
    for s in sells:
        try:
            sell_dt = datetime.strptime(s["date"], "%Y-%m-%d")
            days_ago = (ref_dt - sell_dt).days
            if 0 <= days_ago <= 30:
                recent.append((days_ago, s))
        except Exception:
            continue
    if not recent:
        return None
    # 取最近一次（days_ago 最小）
    recent.sort(key=lambda x: x[0])
    days_ago, last_sell = recent[0]
    return {
        "date": last_sell["date"],
        "exit_price": last_sell["sell_price"],
        "days_ago": days_ago,
    }


def _enrich_holdings_with_market_data(
    active_holdings: dict[str, dict[str, Any]],
    repo: "RadarDataRepo",
) -> dict[str, dict[str, Any]]:
    """用最新市场数据丰富持仓信息：计算动态市值、总资产、实际占比、浮盈。

    返回 enriched: {ticker: {..., current_price, current_value, pnl_pct, actual_weight, cash_balance}}
    如果 holdings.csv 中有 CASH 行，优先使用其 Quantity 作为现金余额。
    """
    enriched: dict[str, dict[str, Any]] = {}

    # 读取 CASH 行
    cash_balance = 0.0
    try:
        if HOLDINGS_CSV.is_file():
            df = pd.read_csv(HOLDINGS_CSV)
            df.columns = df.columns.str.strip()
            cash_rows = df[df["Ticker"].str.strip().str.upper() == "CASH"]
            if not cash_rows.empty:
                cash_qty = float(cash_rows.iloc[-1].get("Buy_Qty", 0) or 0)
                if cash_qty > 0:
                    cash_balance = cash_qty
    except Exception:
        pass

    # 计算每只正股的最新市场价格
    total_stock_value = 0.0
    ticker_prices: dict[str, float] = {}
    for ticker in active_holdings:
        row = repo.get_latest_ticker_metrics(ticker)
        if row and row.get("Close_Price", 0) > 0:
            ticker_prices[ticker] = float(row["Close_Price"])
        else:
            ticker_prices[ticker] = active_holdings[ticker].get("avg_cost", 0)

    # 先算全部股票市值
    for ticker, pos in active_holdings.items():
        current_price = ticker_prices.get(ticker, pos["avg_cost"])
        current_value = current_price * pos["total_qty"]
        total_stock_value += current_value

    total_nav = total_stock_value + cash_balance

    # 构建 enriched
    for ticker, pos in active_holdings.items():
        current_price = ticker_prices.get(ticker, pos["avg_cost"])
        current_value = current_price * pos["total_qty"]
        avg_cost = pos["avg_cost"]
        pnl_pct = (current_price - avg_cost) / avg_cost if avg_cost > 0 else 0.0
        actual_weight = current_value / total_nav if total_nav > 0 else 0.0

        enriched[ticker] = {
            **pos,
            "current_price": current_price,
            "current_value": current_value,
            "pnl_pct": pnl_pct,
            "actual_weight": actual_weight,
            "cash_balance": cash_balance,
            "total_nav": total_nav,
        }

    return enriched


# ============================================================================
# 动态凯利公式资金管理
# ============================================================================

def _calculate_kelly_weight(
    ticker: str,
    row: dict[str, Any],
    macro_state: dict[str, Any],
    washout_triggered: bool = False,
) -> dict[str, Any]:
    """动态凯利公式：Target_Weight = (b * p - q) / b，半仓凯利 + 单股硬顶 20%。

    Args:
        ticker: 标的代码
        row: ticker_metrics 最新记录
        macro_state: 大盘状态字典
        washout_triggered: 是否触发模块四防洗飞修复路径

    Returns:
        {
            "b": float,       # 净赔率（RR_Score）
            "p": float,       # 动态胜率
            "target_weight": float,
            "suggested_weight": float,  # 半仓凯利
            "final_target_weight": float,  # min(suggested, 0.20)
        }
    """
    # b（净赔率）= RR_Score
    rr_score = row.get("RR_Score")
    if rr_score is None or rr_score <= 0:
        return {"b": 0, "p": 0, "target_weight": 0.0, "suggested_weight": 0.0, "final_target_weight": 0.0}
    b = float(rr_score)

    # p（动态胜率）：基础 40%
    p = 0.40

    # 大盘状态加成：bull_core 或 neutral_bull → +15%
    spy_regime = macro_state.get("spy_regime", "unknown")
    if spy_regime in MACRO_BULL_STATES:
        p += 0.15

    # 个股趋势加成：bull_core / bull_strong → +10%
    trend_state = row.get("Trend_State", "unknown")
    if trend_state in ("bull_core", "bull_strong"):
        p += 0.10

    # 防洗飞修复路径加成 → +10%
    if washout_triggered:
        p += 0.10

    # 胜率上限 75%
    p = min(p, 0.75)

    q = 1.0 - p

    # 凯利公式
    if b <= 0:
        target_weight = 0.0
    else:
        target_weight = (b * p - q) / b

    target_weight = max(target_weight, 0.0)

    # 半仓凯利
    suggested_weight = target_weight * 0.5

    # 单股硬顶 20%
    final_target_weight = min(suggested_weight, 0.20)

    return {
        "b": b,
        "p": p,
        "target_weight": target_weight,
        "suggested_weight": suggested_weight,
        "final_target_weight": final_target_weight,
    }


def _read_watchlist_symbols() -> list[str]:
    """从 data/watchlist.csv 读取所有 ticker（逗号/换行分隔）。"""
    p = BASE_DIR / "data" / "watchlist.csv"
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8").strip()
    tokens = []
    for line in text.split("\n"):
        for t in line.split(","):
            t = t.strip().upper()
            if t:
                tokens.append(t)
    return tokens


# ============================================================================
# 数据库访问层
# ============================================================================


class RadarDataRepo:
    """为 strategy_radar 提供精简数据访问。

    支持 as_of_date 回测模式：所有查询仅包含 date <= as_of_date 的数据，
    模拟指定日期的历史状态。
    """

    def __init__(self, db_path: pathlib.Path, as_of_date: str | None = None):
        self.db_path = db_path
        self.as_of_date = as_of_date  # "YYYY-MM-DD" 或 None（最新）

    def _date_filter(self, alias: str = "date") -> str:
        """生成 SQL 日期过滤子句。"""
        if self.as_of_date:
            return f" AND {alias} <= '{self.as_of_date}'"
        return ""

    def get_latest_macro_state(self) -> dict[str, Any]:
        """获取最新的大盘状态（双重校验：SPY + QQQ）。"""
        result: dict[str, Any] = {
            "spy_regime": "unknown",
            "qqq_regime": "unknown",
            "spy_ma200": None,
            "qqq_ma200": None,
        }
        date_filter = self._date_filter()

        # 获取 SPY 状态
        with sqlite3.connect(self.db_path) as conn:
            try:
                cur = conn.execute(
                    f"SELECT metrics FROM quantitative WHERE ticker='SPY' {date_filter} ORDER BY date DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    data = json.loads(row[0])
                    mc = data.get("market_context", {})
                    trend = mc.get("spy_trend", {}) or data.get("trend", {})
                    result["spy_regime"] = mc.get("spy_trend_regime",
                                                  trend.get("regime", "unknown"))
                    mas = mc.get("spy_ma", {})
                    result["spy_ma200"] = mas.get("ma_200")
                    result["spy_ma20"] = mas.get("ma_20")
                    result["spy_ma50"] = mas.get("ma_50")
            except Exception:
                pass

        # 获取 QQQ 状态
        with sqlite3.connect(self.db_path) as conn:
            try:
                cur = conn.execute(
                    f"SELECT metrics FROM quantitative WHERE ticker='QQQ' {date_filter} ORDER BY date DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    data = json.loads(row[0])
                    mc = data.get("market_context", {})
                    trend = mc.get("qqq_trend", {}) or data.get("trend", {})
                    result["qqq_regime"] = mc.get("qqq_trend_regime",
                                                  trend.get("regime", "unknown"))
                    mas = mc.get("qqq_ma", {})
                    result["qqq_ma200"] = mas.get("ma_200")
                    result["qqq_ma20"] = mas.get("ma_20")
                    result["qqq_ma50"] = mas.get("ma_50")
            except Exception:
                pass

        if result["spy_regime"] == "unknown":
            result["spy_regime"] = self._infer_spy_regime_from_any()
        if result["qqq_regime"] == "unknown":
            result["qqq_regime"] = self._infer_qqq_regime_from_any()
        return result


    def _infer_spy_regime_from_any(self) -> str:
        """从任意标的的 market_context 中推断 SPY regime。"""
        date_filter = self._date_filter()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"SELECT metrics FROM quantitative WHERE 1=1 {date_filter} ORDER BY date DESC LIMIT 5"
            )
            for (row,) in cur.fetchall():
                try:
                    data = json.loads(row)
                    mc = data.get("market_context", {})
                    regime = mc.get("spy_trend_regime")
                    if regime:
                        return regime
                except Exception:
                    continue
        return "unknown"

    def _infer_qqq_regime_from_any(self) -> str:
        """从任意标的的 market_context 中推断 QQQ regime。"""
        date_filter = self._date_filter()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"SELECT metrics FROM quantitative WHERE 1=1 {date_filter} ORDER BY date DESC LIMIT 5"
            )
            for (row,) in cur.fetchall():
                try:
                    data = json.loads(row)
                    mc = data.get("market_context", {})
                    regime = mc.get("qqq_trend_regime")
                    if regime:
                        return regime
                except Exception:
                    continue
        return "unknown"

    def get_ticker_metrics_history(self, ticker: str, days: int = 30) -> pd.DataFrame:
        """获取 ticker_metrics 表的近期历史（用于信号扭转检测）。"""
        date_filter = self._date_filter("tm.Date")
        query = f"""
            SELECT Date, Close_Price, Trend_State, RSI, IV_Rank, Crowding_Index,
                   Win_Prob_10d, Win_Prob_20d, Win_Prob_60d,
                   Risk_Loss_10d, Risk_Loss_20d, Risk_Loss_60d,
                   ATR_14, Hard_Stop_Loss, Target_Aggressive, RR_Score,
                   PE_Percentile, IV_Status, SPY_State, Alpha_vs_SPY, Corr_vs_SPY,
                   Action, Entry_Ref, Status
            FROM ticker_metrics tm
            WHERE tm.Ticker = ? {date_filter}
            ORDER BY Date DESC
            LIMIT ?
        """
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query(query, conn, params=(ticker, days))
        if df.empty:
            return df
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.sort_values("Date").reset_index(drop=True)
        return df

    def get_latest_ticker_metrics(self, ticker: str) -> Optional[dict[str, Any]]:
        """获取最新一条 ticker_metrics 记录。"""
        df = self.get_ticker_metrics_history(ticker, days=1)
        if df.empty:
            return None
        return df.iloc[-1].to_dict()

    def get_all_tickers(self) -> list[str]:
        """获取 ticker_metrics 中所有 ticker。"""
        date_filter = self._date_filter()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"SELECT DISTINCT Ticker FROM ticker_metrics WHERE 1=1 {date_filter} ORDER BY Ticker"
            )
            return [r[0] for r in cur.fetchall()]

    def get_all_tickers_latest(self) -> dict[str, dict[str, Any]]:
        """获取所有 ticker 最新一条数据，返回 {ticker: row_dict}。"""
        result = {}
        all_tickers = self.get_all_tickers()
        for t in all_tickers:
            row = self.get_latest_ticker_metrics(t)
            if row:
                result[t] = row
        return result

    def get_peg_ratio(self, ticker: str) -> float | None:
        """从 fundamentals 表查询 PEG 比率（forwardPE / EPS_growth*100）。

        若无 forwardPE 则回退到 trailingPE。缺失数据时返回 None。
        """
        date_filter = self._date_filter()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"SELECT data FROM fundamentals WHERE ticker=? {date_filter} ORDER BY date DESC LIMIT 1",
                (ticker,),
            )
            row = cur.fetchone()
            if not row:
                return None
            try:
                data = json.loads(row[0])
                cr = data.get("current_ratios", {})
                eps_growth = cr.get("growth_indicators", {}).get("eps_growth")
                forward_pe = cr.get("valuation_ratios", {}).get("forward_pe")
                trailing_pe = cr.get("valuation_ratios", {}).get("pe_ratio")
                if eps_growth and isinstance(eps_growth, (int, float)) and eps_growth > 0:
                    pe = forward_pe if forward_pe else trailing_pe
                    if pe and isinstance(pe, (int, float)) and pe > 0:
                        return float(pe) / (float(eps_growth) * 100)
            except Exception:
                pass
        return None

    def get_percentile_thresholds(self, ticker: str,
                                  lookup_days: int = 504) -> dict[str, float]:
        """计算指定标的 RSI 和 Crowding_Index 的 15% 历史低位分位数。

        基于 lookup_days（默认约 2 年交易日）内的完整 ticker_metrics 历史，
        返回 {"rsi_p15": float, "crowding_p15": float}。

        若数据不足则回退到绝对阈值（RSI≤33, Crowding≤8.5）。
        """
        fallback = {"rsi_p15": 33.0, "crowding_p15": 8.5}
        try:
            df = self.get_ticker_metrics_history(ticker, days=lookup_days)
            if df.empty or len(df) < 20:
                return fallback
            rsi_vals = df["RSI"].dropna()
            crowd_vals = df["Crowding_Index"].dropna()
            if len(rsi_vals) < 20 or len(crowd_vals) < 20:
                return fallback
            return {
                "rsi_p15": float(np.percentile(rsi_vals, PERCENTILE_LOW)),
                "crowding_p15": float(np.percentile(crowd_vals, PERCENTILE_LOW)),
            }
        except Exception:
            return fallback


# ============================================================================
# 模块一：宏观熔断器与标的筛选
# ============================================================================


class MacroGatekeeper:
    """大盘状态监控 + 熔断决策。对 watchlist 中所有标的进行监控。"""

    def __init__(self, repo: RadarDataRepo):
        self.repo = repo
        self.macro_state: dict[str, Any] = {}
        self.system_status: str = "UNKNOWN"
        self.monitored_tickers: list[str] = []
        self.excluded_tickers: dict[str, str] = {}  # ticker -> reason

    def evaluate(self, watchlist: list[str]) -> dict[str, Any]:
        """执行宏观评估，返回状态摘要。

        单点校验逻辑：只需 SPY 处于 ['bull_core', 'neutral_bull'] 即可 OPERATIONAL。

        Args:
            watchlist: 从 watchlist.csv 读取的完整标的列表
        """
        self.macro_state = self.repo.get_latest_macro_state()
        spy_regime = self.macro_state.get("spy_regime", "unknown")

        # 【单点校验】：仅 SPY 决定
        if spy_regime in MACRO_BULL_STATES:
            self.system_status = "OPERATIONAL"
        else:
            self.system_status = "MELTDOWN"

        # 构建监控列表：watchlist 中所有在 ticker_metrics 有数据的标的
        self._build_monitored_list(watchlist)

        return {
            "system_status": self.system_status,
            "spy_regime": spy_regime,
            "qqq_regime": self.macro_state.get("qqq_regime", "unknown"),
            "spy_ma200": self.macro_state.get("spy_ma200"),
            "spy_ma20": self.macro_state.get("spy_ma20"),
            "spy_ma50": self.macro_state.get("spy_ma50"),
            "qqq_ma200": self.macro_state.get("qqq_ma200"),
            "qqq_ma20": self.macro_state.get("qqq_ma20"),
            "qqq_ma50": self.macro_state.get("qqq_ma50"),
            "monitored_count": len(self.monitored_tickers),
            "excluded": self.excluded_tickers,
        }

    def _build_monitored_list(self, watchlist: list[str]) -> None:
        """从 watchlist 构建监控列表：有 ticker_metrics 数据的全部纳入。"""
        all_latest = self.repo.get_all_tickers_latest()

        for ticker in sorted(watchlist):
            if ticker not in all_latest:
                self.excluded_tickers[ticker] = "ticker_metrics 无数据"
                continue
            self.monitored_tickers.append(ticker)

        if not self.monitored_tickers:
            self.system_status = "MELTDOWN"
            self.excluded_tickers["_system"] = "无任何标的有 ticker_metrics 数据"

    def is_operational(self) -> bool:
        return self.system_status == "OPERATIONAL"


# ============================================================================
# 模块二：Reverse_Core_Buy_Protocol（左侧黄金建仓）
# ============================================================================


class ReverseCoreBuyProtocol:
    """三维极值触发矩阵 + 左侧建仓执行 + 硬止损。"""

    def __init__(self, repo: RadarDataRepo):
        self.repo = repo

    def scan_all(self, tickers: list[str]) -> list[dict[str, Any]]:
        """对所有标的执行三维检索，返回触发了左侧信号的标的列表。"""
        signals = []
        for ticker in tickers:
            result = self.evaluate_ticker(ticker)
            if result["signal_triggered"]:
                signals.append(result)
        return signals

    def evaluate_ticker(self, ticker: str) -> dict[str, Any]:
        """对单个标的执行三维极值触发矩阵检测（3日时空共振版）。

        严苛标准：
        - 估值轴：PE_Percentile ≤ 15%（窗口内任意一天）
        - 动能轴：RSI ≤ 33 AND Crowding ≤ 5.0（窗口内任意一天）
        - 概率轴：Win_Prob_10d 从 0% 冰点首次向上扭转（> 5%，必须今天触发）

        只要估值/动能在 3 日滚动窗口内亮灯，且【今天】概率轴扭转 → 共振信号触发。
        """
        # 取足够历史（窗口天数 + 5 天余量）
        df = self.repo.get_ticker_metrics_history(ticker,
                                                  days=LEFT_MATRIX_WINDOW_DAYS + 5)

        result = {
            "ticker": ticker,
            "signal_triggered": False,
            "valuation_ok": False,
            "momentum_ok": False,
            "probability_ok": False,
            "detail_valuation": "",
            "detail_momentum": "",
            "detail_probability": "",
            "entry_price": None,
            "left_stop_loss": None,
            "position_size": None,
            "position_range": f"{LEFT_ENTRY_POSITION_MIN:.0%}-{LEFT_ENTRY_POSITION_MAX:.0%}",
            "atr_14": None,
        }

        if df.empty or len(df) < 2:
            result["detail_valuation"] = "数据不足"
            result["detail_momentum"] = "数据不足"
            result["detail_probability"] = "数据不足"
            return result

        latest = df.iloc[-1]
        close_price = latest.get("Close_Price", 0)
        atr = latest.get("ATR_14", 0) or 0
        result["atr_14"] = atr

        # ---- 估值轴：90日滚动价格分位 ≤ 15%（窗口内任意一天） ----
        # 直接使用价格时间序列分位，忽略 DB 中可能过时/静态的 PE_Percentile
        # 取 90 行历史用于价格分位计算（与 analyze_report.py 的 PE_WINDOW_DAYS=90 对齐）
        df_wide = self.repo.get_ticker_metrics_history(ticker, days=90)
        window_df = df.tail(LEFT_MATRIX_WINDOW_DAYS + 1)  # 含今天（动能/概率轴用）
        pe_db = window_df["PE_Percentile"].dropna()
        pe_db_str = f"PE静态 {float(pe_db.iloc[-1]):.1f}%" if len(pe_db) > 0 else "PE无数据"
        all_prices = df_wide["Close_Price"].dropna()
        if len(all_prices) >= 5:
            pe_min = 100.0 * float((all_prices <= close_price).mean())
            detail = (
                f"价格分位 {pe_min:.1f}%（{pe_db_str}，阈值 ≤{PE_PERCENTILE_THRESHOLD}%）"
            )
            if pe_min <= PE_PERCENTILE_THRESHOLD:
                result["valuation_ok"] = True
                result["detail_valuation"] = f"{detail} ✓"
            else:
                result["detail_valuation"] = f"{detail} ✗"
        else:
            result["detail_valuation"] = "价格数据不足，跳过估值轴"

        # ---- 估值轴通道B：PEG 估值（高成长标的专用通道） ----
        if not result["valuation_ok"]:
            peg = self.repo.get_peg_ratio(ticker)
            if peg is not None and peg <= PEG_THRESHOLD:
                result["valuation_ok"] = True
                result["detail_valuation"] = (
                    f"PEG={peg:.2f} ≤ {PEG_THRESHOLD}（高成长估值通道）✓"
                )
            elif peg is not None:
                tag = f"PEG={peg:.2f} > {PEG_THRESHOLD}"
                if result["detail_valuation"]:
                    result["detail_valuation"] += f"；{tag}"
                else:
                    result["detail_valuation"] = tag + " ✗"

        # ---- 动能轴：RSI ≤ 33 且 Crowding ≤ 5.0（硬编码严苛标准，窗口内任意一天） ----
        window_df_valid = window_df.dropna(subset=["RSI", "Crowding_Index"])
        if not window_df_valid.empty:
            rsi_ok_any = (window_df_valid["RSI"] <= RSI_THRESHOLD).any()
            crowd_ok_any = (window_df_valid["Crowding_Index"] <= CROWDING_THRESHOLD).any()
            if rsi_ok_any and crowd_ok_any:
                result["momentum_ok"] = True
                # 找到同时满足条件的最近日
                both_ok = window_df_valid[
                    (window_df_valid["RSI"] <= RSI_THRESHOLD) &
                    (window_df_valid["Crowding_Index"] <= CROWDING_THRESHOLD)
                ]
                best_day = both_ok.iloc[0] if not both_ok.empty else window_df_valid.iloc[-1]
                best_date = str(best_day.get("Date", "?"))[:10]
                rsi_val = best_day.get("RSI", 0)
                crowd_val = best_day.get("Crowding_Index", 0)
                result["detail_momentum"] = (
                    f"RSI(14)={rsi_val:.1f} ≤ {RSI_THRESHOLD:.0f} ✓ & "
                    f"Crowding={crowd_val:.1f} ≤ {CROWDING_THRESHOLD:.1f} ✓（窗口 {best_date}）"
                )
            else:
                parts = []
                if not rsi_ok_any:
                    rsi_min = window_df_valid["RSI"].min()
                    parts.append(f"RSI最低 {rsi_min:.1f} > {RSI_THRESHOLD:.0f} ✗")
                if not crowd_ok_any:
                    crowd_min = window_df_valid["Crowding_Index"].min()
                    parts.append(f"Crowding最低 {crowd_min:.1f} > {CROWDING_THRESHOLD:.1f} ✗")
                result["detail_momentum"] = "; ".join(parts)
        else:
            result["detail_momentum"] = "动能数据缺失"

        # ---- 概率轴：双路径检测 ----
        # 路径P1: Win_Prob_10d 从 0% 冰点首次向上扭转（> 5%）
        # 路径P2: Win_Prob_10d 单日回升脉冲（当日-前日 ≥ MIN_SPIKE）
        prob_ok_p1, detail_p1 = self._check_win_prob_flip(df, ticker)
        prob_ok_p2, detail_p2 = self._check_win_prob_spike(df, ticker)
        result["probability_ok"] = prob_ok_p1 or prob_ok_p2
        if result["probability_ok"]:
            result["detail_probability"] = detail_p1 if prob_ok_p1 else detail_p2
        else:
            result["detail_probability"] = f"未触发：{detail_p1}；{detail_p2}"

        # ---- 综合信号判断：估值/动能在窗口内亮灯 + 今天概率轴扭转 → 共振 ----
        if result["valuation_ok"] and result["momentum_ok"] and result["probability_ok"]:
            result["signal_triggered"] = True
            result["entry_price"] = close_price

            if atr > 0 and close_price > 0:
                result["left_stop_loss"] = close_price - LEFT_ATR_MULTIPLIER * atr
            else:
                result["left_stop_loss"] = close_price * 0.90

            result["position_size"] = LEFT_ENTRY_POSITION_MIN

        return result

    def _check_win_prob_flip(self, df: pd.DataFrame, ticker: str) -> tuple[bool, str]:
        """检测 Win_Prob_10d 的"昨天为0，今天>5%"交点信号。

        这是策略核心逻辑：只抓取从冰点（0%）首次向上扭转的第一信号，
        避免误判已经反弹到高位的后续数据。
        """
        if len(df) < 2:
            return False, "数据不足（需≥2条记录进行 shift 比较）"

        # 获取最近两天的 Win_Prob_10d
        win_prob_col = "Win_Prob_10d"
        if win_prob_col not in df.columns:
            return False, "Win_Prob_10d 字段缺失"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        prev_val = prev.get(win_prob_col)
        curr_val = curr.get(win_prob_col)

        # 处理 None/NaN
        def _valid(v):
            if v is None:
                return None
            try:
                fv = float(v)
                if math.isnan(fv):
                    return None
                return fv
            except (ValueError, TypeError):
                return None

        prev_prob = _valid(prev_val)
        curr_prob = _valid(curr_val)

        if prev_prob is None or curr_prob is None:
            return False, f"Win_Prob_10d 数据缺失 (prev={prev_val}, curr={curr_val})"

        # 核心逻辑：昨天=0 且 今天>5%
        prev_is_zero = abs(prev_prob) < 0.001  # 浮点容差
        curr_above_threshold = curr_prob > WIN_PROB_FLIP_THRESHOLD / 100.0

        if prev_is_zero and curr_above_threshold:
            prev_date = str(df.iloc[-2].get("Date", "?"))[:10]
            curr_date = str(df.iloc[-1].get("Date", "?"))[:10]
            return True, (
                f"Win_Prob_10d 首次扭转: {prev_date} = {prev_prob:.1%} → "
                f"{curr_date} = {curr_prob:.1%} (> {WIN_PROB_FLIP_THRESHOLD}%) ✓"
            )
        elif prev_is_zero and not curr_above_threshold:
            return False, (
                f"Win_Prob_10d 昨日为0但今日仅 {curr_prob:.1%}，未达 {WIN_PROB_FLIP_THRESHOLD}% 阈值"
            )
        elif curr_above_threshold and not prev_is_zero:
            return False, (
                f"Win_Prob_10d 昨日={prev_prob:.1%}（非0），非首次冰点扭转（可能已反弹至高位）"
            )
        else:
            return False, (
                f"Win_Prob_10d 昨日={prev_prob:.1%}，今日={curr_prob:.1%}，未触发扭转条件"
            )

    def _check_win_prob_spike(self, df: pd.DataFrame, ticker: str) -> tuple[bool, str]:
        """检测 Win_Prob_10d 单日回升脉冲。

        当价格持续下跌后企稳，Win_Prob 可能从未回到 0%（TSLA 典型场景），
        此时用"当日-前日 回升幅度 ≥ MIN_SPIKE"作为替代信号。
        """
        if len(df) < 2:
            return False, "数据不足"
        win_prob_col = "Win_Prob_10d"
        if win_prob_col not in df.columns:
            return False, "字段缺失"

        prev_val = df.iloc[-2].get(win_prob_col)
        curr_val = df.iloc[-1].get(win_prob_col)

        def _v(v):
            try:
                f = float(v)
                return f if not math.isnan(f) else None
            except Exception:
                return None

        prev = _v(prev_val)
        curr = _v(curr_val)
        if prev is None or curr is None:
            return False, f"数据缺失 (prev={prev_val}, curr={curr_val})"

        spike_pct = (curr - prev) * 100.0
        prev_date = str(df.iloc[-2].get("Date", "?"))[:10]
        curr_date = str(df.iloc[-1].get("Date", "?"))[:10]

        if spike_pct >= WIN_PROB_MIN_SPIKE / 100.0:
            return True, (
                f"Win_Prob 回升脉冲: {prev_date}={prev:.1%} → "
                f"{curr_date}={curr:.1%} (Δ={spike_pct:+.1f}pp, ≥{WIN_PROB_MIN_SPIKE/100:.1f}pp) ✓"
            )
        return False, (
            f"Win_Prob 回升脉冲不足: Δ={spike_pct:+.1f}pp < {WIN_PROB_MIN_SPIKE/100:.1f}pp"
        )


# ============================================================================
# 模块三：右侧加仓与风险追踪
# ============================================================================


class RightSideAddition:
    """右侧加仓前置条件检查 + 止损收紧。"""

    @staticmethod
    def evaluate_addition(ticker: str, repo: RadarDataRepo,
                          left_entry_price: float,
                          left_stop_loss: float) -> dict[str, Any]:
        """评估是否满足右侧加仓条件。

        前置：
        - IV 不能处于 Expensive 区间
        - 价格回踩确认支撑 或 趋势状态转为右侧多头排列
        """
        row = repo.get_latest_ticker_metrics(ticker)
        if not row:
            return {"ready": False, "reason": "无数据", "main_stop_loss": None, "atr_14": None}

        iv_status = row.get("IV_Status", "unknown")
        trend_state = row.get("Trend_State", "unknown")
        close_price = row.get("Close_Price", 0)
        atr = row.get("ATR_14", 0) or 0

        reasons = []

        # IV 检查
        if iv_status == "expensive":
            return {
                "ready": False,
                "reason": f"IV 处于 Expensive（高昂）区间，不适合右侧加仓",
                "iv_status": iv_status,
                "main_stop_loss": None,
                "atr_14": atr,
            }

        # 趋势检查：bear_core → 右侧多头排列
        bull_states = {"bull_core", "bull_strong", "bull_partial", "neutral_bull"}
        trend_ok = trend_state in bull_states

        if trend_ok:
            reasons.append(f"趋势状态 '{trend_state}' 已转为多头排列 ✓")
        else:
            reasons.append(f"趋势状态 '{trend_state}' 尚未确认右侧多头")

        # 计算收紧后的主力止损
        entry_combined = left_entry_price  # 简化：使用左侧入场价
        main_stop_loss = None
        if atr > 0 and close_price > 0:
            atr_stop = entry_combined - RIGHT_ATR_MULTIPLIER * atr
            # 取 min(ATR止损, 关键支撑) —— 这里用简单均线作为支撑代理
            main_stop_loss = atr_stop

        ready = trend_ok

        return {
            "ready": ready,
            "reason": " | ".join(reasons) if reasons else "待确认",
            "iv_status": iv_status,
            "trend_state": trend_state,
            "close_price": close_price,
            "entry_combined": entry_combined,
            "main_stop_loss": main_stop_loss,
            "atr_14": atr,
        }


# ============================================================================
# 模块四：防洗飞与右侧回补协议
# ============================================================================


class AntiWashoutProtocol:
    """解决被动止损后强牛股 V 反洗飞的痛点。

    双路径量化认错买回：路径A（动能修复-防假跌破）和路径B（指标共振修复-防低位V反）。
    """

    def __init__(self, repo: RadarDataRepo):
        self.repo = repo

    def evaluate(self, ticker: str, system_status: str,
                 last_stop_out: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """评估防洗飞回补条件。

        Args:
            ticker: 标的代码
            system_status: 系统状态
            last_stop_out: 最近一次止损记录 {'date': str, 'exit_price': float, 'days_ago': int}

        Returns:
            评估结果字典
        """
        result = {
            "activated": False,
            "path_a_triggered": False,
            "path_b_triggered": False,
            "recommendation": "",
        }

        # 前置条件1：大盘必须处于 OPERATIONAL
        if system_status != "OPERATIONAL":
            result["recommendation"] = "大盘未处于 bull_core/neutral_bull，不激活防洗飞协议"
            return result

        # 前置条件2：必须有止损记录
        if last_stop_out is None:
            result["recommendation"] = "无近期止损记录，不适用防洗飞协议"
            return result

        days_ago = last_stop_out.get("days_ago", 999)
        if days_ago > WASHOUT_LOOKBACK_N:
            result["recommendation"] = (f"最近止损发生在 {days_ago} 天前，"
                                       f"超过 {WASHOUT_LOOKBACK_N} 天窗口")
            return result

        entry_ref = last_stop_out["exit_price"]
        result["activated"] = True
        result["entry_ref"] = entry_ref
        result["days_since_stop"] = days_ago

        # 获取最新数据
        row = self.repo.get_latest_ticker_metrics(ticker)
        if not row:
            result["recommendation"] = "无最新行情数据"
            return result

        close_price = row.get("Close_Price", 0)
        atr = row.get("ATR_14", 0) or 1
        rsi = row.get("RSI")
        trend_state = row.get("Trend_State", "unknown")

        # ---- 路径 A：动能修复 - 防假跌破 ----
        path_a = self._evaluate_path_a(ticker, close_price, atr, entry_ref, row)
        result["path_a_triggered"] = path_a["triggered"]
        result["path_a_detail"] = path_a["detail"]

        # ---- 路径 B：指标共振修复 - 防低位直接V反 ----
        path_b = self._evaluate_path_b(ticker, close_price, entry_ref, rsi, row)
        result["path_b_triggered"] = path_b["triggered"]
        result["path_b_detail"] = path_b["detail"]

        # 综合建议
        if path_a["triggered"] or path_b["triggered"]:
            triggered = []
            if path_a["triggered"]:
                triggered.append("路径A（动能修复）")
            if path_b["triggered"]:
                triggered.append("路径B（指标共振）")
            result["recommendation"] = (
                f"🟢 防洗飞回补信号触发！满足 {' + '.join(triggered)}，"
                f"建议在 ${close_price:.2f} 附近重新建仓"
            )
        else:
            result["recommendation"] = "未满足双路径触发条件，继续观望"

        return result

    def _evaluate_path_a(self, ticker: str, close_price: float,
                         atr: float, entry_ref: float,
                         row: dict[str, Any]) -> dict[str, Any]:
        """路径A：动态 alpha + 连续 2 日确认规则。

        - 动态 alpha：若 ATR(14) / Price > 4.0%，则 alpha = 0.015；否则 alpha = 0.010
        - 触发阈值：Entry_Ref × (1 + alpha)
        - 确认条件：必须连续 2 个交易日站上该阈值
        """
        # 计算动态 α
        if atr > 0 and close_price > 0:
            atr_ratio = atr / close_price
        else:
            atr_ratio = 0

        if atr_ratio > WASHOUT_HIGH_VOL_THRESHOLD:
            alpha = WASHOUT_ALPHA_HIGH_VOL
            vol_status = "高波动"
        else:
            alpha = WASHOUT_ALPHA_STANDARD
            vol_status = "标准波动"

        threshold_price = entry_ref * (1.0 + alpha)

        detail = (f"[{vol_status}] α={alpha:.1%} 触发阈值 = ${entry_ref:.2f} × (1+{alpha:.1%}) = "
                  f"${threshold_price:.2f}，当前价 ${close_price:.2f}")

        if close_price < threshold_price:
            detail += f" → 未达标（还需上涨 ${threshold_price - close_price:.2f}）"
            return {"triggered": False, "detail": detail}

        # ---- 确认条件：连续 2 日站上阈值 ----
        df = self.repo.get_ticker_metrics_history(ticker, days=5)
        if len(df) >= 2:
            last_two = df.iloc[-2:]
            prices = last_two["Close_Price"].values
            consecutive_above = all(p >= threshold_price for p in prices)
            if consecutive_above:
                dates = last_two["Date"].apply(lambda x: str(x)[:10]).values
                detail += f" | 连续2日(${prices[-2]:.2f}, ${prices[-1]:.2f})站上阈值 ✓"
                return {"triggered": True, "detail": detail}
            else:
                detail += f" | 连续2日未完全确认（{prices[-2]:.2f}, {prices[-1]:.2f}）"
                return {"triggered": False, "detail": detail}

        detail += " | 历史数据不足，待 2 日确认"
        return {"triggered": False, "detail": detail}

    def _evaluate_path_b(self, ticker: str, close_price: float,
                         entry_ref: float, rsi: Optional[float],
                         row: dict[str, Any]) -> dict[str, Any]:
        """路径B：指标共振修复 - 防低位直接 V 反。

        条件：
        1. RSI 从 ≤ 33 冰点区强力向上突破 40（RSI_current >= 40）
        2. 大盘当天为阳线（SPY/QQQ 下跌则不符）
        """
        conditions = []

        # 条件1：RSI 从 ≤ 33 冰点区向上突破 40
        df = self.repo.get_ticker_metrics_history(ticker, days=5)
        rsi_breakthrough = False
        if rsi is not None and len(df) >= 2:
            prev_rsi = df.iloc[-2].get("RSI")
            curr_rsi = rsi
            if (prev_rsi is not None and prev_rsi <= 33 and
                curr_rsi is not None and curr_rsi >= WASHOUT_RSI_REBOUND):
                rsi_breakthrough = True
                conditions.append(f"RSI 从 {prev_rsi:.1f}(≤33) 突破至 {curr_rsi:.1f}(≥40) ✓")
            elif curr_rsi >= WASHOUT_RSI_REBOUND:
                conditions.append(f"RSI 当前 {curr_rsi:.1f} ≥ {WASHOUT_RSI_REBOUND}（需确认前日 ≤ 33）⚠️")
            else:
                conditions.append(f"RSI(14)={curr_rsi:.1f} < {WASHOUT_RSI_REBOUND} ✗")
        else:
            conditions.append("RSI 数据不足或缺失 ✗")

        # 条件2：大盘当天为阳线（SPY/QQQ 当天上涨）
        macro = self.repo.get_latest_macro_state()
        spy_regime = macro.get("spy_regime", "unknown")
        qqq_regime = macro.get("qqq_regime", "unknown")
        market_bull = spy_regime in MACRO_BULL_STATES and qqq_regime in MACRO_BULL_STATES
        if market_bull:
            conditions.append(f"大盘多头（SPY:{spy_regime} + QQQ:{qqq_regime}）✓")
        else:
            conditions.append(f"大盘非多头（SPY:{spy_regime}, QQQ:{qqq_regime}）✗")

        all_met = rsi_breakthrough and market_bull
        detail = " | ".join(conditions)

        return {"triggered": all_met, "detail": detail}


# ============================================================================
# 模块五：多维阶梯式出场与动态资金管理协议
# ============================================================================


class MultiStageExitProtocol:
    """四路径多维阶梯式出场协议，结合市场量化指标与账户真实持仓深度绑定。

    路径 A: 动态盈亏比恶化退出
    路径 B: 重仓股动能高位衰竭（仓位占比与RSI强绑定）
    路径 C: 实际持仓与凯利权重严重偏离纠偏
    路径 D: 成本线/均线右侧破位硬保护
    """

    def evaluate(
        self,
        ticker: str,
        enriched: dict[str, Any],
        row: dict[str, Any],
        kelly: dict[str, Any],
        system_status: str,
        pe_percentile: float | None = None,
        cash_balance: float = 0.0,
        left_signal_exists: bool = False,
    ) -> dict[str, Any]:
        """对单个持仓标的执行四路径阶梯式出场评估（长线价值导向版）。

        Args:
            ticker: 标的代码
            enriched: 丰富后的持仓数据
            row: ticker_metrics 最新记录
            kelly: _calculate_kelly_weight 结果
            system_status: 系统状态
            pe_percentile: 最新 PE 分位（用于核心资产估值豁免）
            cash_balance: 账户闲置现金
            left_signal_exists: 该标的当前是否已触发模块二 L1 信号

        Returns:
            触发状态字典
        """
        result: dict[str, Any] = {
            "ticker": ticker,
            "triggered": False,
            "signals": [],
            "path_a": None,
            "path_b": None,
            "path_c": None,
            "path_d": None,
            "primary_action": "",
            "primary_action_tag": "",
        }

        current_price = enriched.get("current_price", 0)
        avg_cost = enriched.get("avg_cost", 0)
        pnl_pct = enriched.get("pnl_pct", 0)
        actual_weight = enriched.get("actual_weight", 0)

        rsi = row.get("RSI")
        target_aggressive = row.get("Target_Aggressive", 0)
        trend_state = row.get("Trend_State", "unknown")
        atr_14 = row.get("ATR_14", 0) or 0

        # ---- 路径 A：动态盈亏比恶化退出 ----
        path_a = self._evaluate_path_a(pnl_pct, current_price, avg_cost, target_aggressive)
        result["path_a"] = path_a
        if path_a["triggered"]:
            result["signals"].append("A")
            result["primary_action"] = path_a["detail"]
            result["primary_action_tag"] = "TAKE_PROFIT_ALL"
            result["triggered"] = True

        # ---- 路径 B：重仓股动能高位衰竭 ----
        path_b = self._evaluate_path_b(rsi, actual_weight, pnl_pct)
        result["path_b"] = path_b
        if path_b["triggered"]:
            result["signals"].append("B")
            if not result["triggered"]:
                result["primary_action"] = path_b["detail"]
                result["primary_action_tag"] = "REDUCE_POSITION"
                result["triggered"] = True

        # ---- 路径 C：凯利权重严重偏离纠偏 ----
        path_c = self._evaluate_path_c(actual_weight, kelly, rsi, pnl_pct)
        result["path_c"] = path_c
        if path_c["triggered"]:
            result["signals"].append("C")
            if not result["triggered"]:
                result["primary_action"] = path_c["detail"]
                result["primary_action_tag"] = "PORTFOLIO_REBALANCE"
                result["triggered"] = True

        # ---- 路径 D：成本线/均线右侧破位硬保护（长线价值导向版） ----
        is_cheap = pe_percentile is not None and pe_percentile <= 20.0
        is_not_cheap = pe_percentile is not None and pe_percentile > 40.0
        spy_meltdown = system_status != "OPERATIONAL"
        path_d = self._evaluate_path_d(
            pnl_pct, current_price, avg_cost, trend_state,
            is_cheap=is_cheap, spy_meltdown=spy_meltdown,
            is_not_cheap=is_not_cheap, pe_percentile=pe_percentile,
            cash_balance=cash_balance, left_signal_exists=left_signal_exists,
            atr_14=atr_14,
        )
        result["path_d"] = path_d
        if path_d["triggered"]:
            result["signals"].append("D")
            if not result["triggered"]:
                result["primary_action"] = path_d["detail"]
                result["primary_action_tag"] = path_d.get("tag", "VALUE_HOLD")
                result["triggered"] = True

        return result

    def _evaluate_path_a(self, pnl_pct: float, current_price: float,
                         avg_cost: float, target_aggressive: float) -> dict[str, Any]:
        """路径 A：动态盈亏比恶化退出（不赚最后一个铜板）。

        触发条件：PNL_Pct > 10% 且 Holding_RR < 0.4。
        Holding_RR = (Target_Aggressive - Current_Price) / (Current_Price - Avg_Cost)
        """
        if pnl_pct <= 0.10:
            return {"triggered": False, "detail": f"浮盈 {pnl_pct:.1%} ≤ 10%，不触发路径A"}

        if not target_aggressive or target_aggressive <= 0 or avg_cost <= 0:
            return {"triggered": False, "detail": "缺少 Target_Aggressive 或 Avg_Cost 数据"}

        upside = target_aggressive - current_price
        profit_base = current_price - avg_cost
        if profit_base <= 0:
            return {"triggered": False, "detail": "当前价未高于成本，不评估持有盈亏比"}

        holding_rr = upside / profit_base
        if holding_rr < 0.4:
            return {
                "triggered": True,
                "detail": (
                    f"[TAKE_PROFIT_ALL] 动态盈亏比恶化：Holding_RR={holding_rr:.2f} < 0.4，"
                    f"剩余空间 ${upside:.2f} 不抵潜在回撤风险 ${profit_base:.2f}，"
                    f"建议全仓清仓或锁定利润。"
                ),
            }

        return {
            "triggered": False,
            "detail": (
                f"Holding_RR={holding_rr:.2f} ≥ 0.4，剩余盈利空间充足"
            ),
        }

    def _evaluate_path_b(self, rsi, actual_weight: float,
                         pnl_pct: float) -> dict[str, Any]:
        """路径 B：重仓股动能高位衰竭（仓位占比与RSI强绑定）。

        Dynamic_RSI_Limit = 80.0 - (Actual_Weight * 100)
        触发条件：RSI >= Dynamic_RSI_Limit 且 PNL_Pct > 0
        """
        if rsi is None:
            return {"triggered": False, "detail": "RSI 数据缺失，跳过路径B"}

        dynamic_rsi_limit = 80.0 - (actual_weight * 100)
        detail = (
            f"Dynamic_RSI_Limit = 80 - {actual_weight:.1%}×100 = {dynamic_rsi_limit:.1f}，"
            f"当前 RSI={rsi:.1f}"
        )

        if rsi >= dynamic_rsi_limit and pnl_pct > 0:
            return {
                "triggered": True,
                "detail": (
                    f"[REDUCE_POSITION] 重仓股触及动态RSI限制：{detail}，"
                    f"浮盈 {pnl_pct:.1%}，高位动能减弱，建议逢高减仓 1/2 或 1/3，向凯利安全权重靠拢。"
                ),
            }
        elif rsi >= dynamic_rsi_limit:
            return {
                "triggered": False,
                "detail": f"{detail}，RSI触及限制但未盈利(PNL={pnl_pct:.1%})，暂不触发",
            }

        return {"triggered": False, "detail": f"{detail}，RSI 低于动态阈值"}

    def _evaluate_path_c(self, actual_weight: float, kelly: dict[str, Any],
                         rsi, pnl_pct: float) -> dict[str, Any]:
        """路径 C：实际持仓与凯利权重严重偏离纠偏。

        触发条件：Actual_Weight > Final_Target_Weight × 1.5 且 RSI > 65
        """
        final_target = kelly.get("final_target_weight", 0)
        if final_target <= 0:
            return {"triggered": False, "detail": "凯利安全权重为0，无法评估偏离"}

        deviation_ratio = actual_weight / final_target
        detail = (
            f"Actual_Weight={actual_weight:.1%} vs Final_Target_Weight={final_target:.1%}，"
            f"偏离倍数={deviation_ratio:.1f}"
        )

        if rsi is None:
            return {"triggered": False, "detail": f"{detail}，RSI数据缺失"}

        rsi_condition = rsi > 65
        weight_condition = actual_weight > final_target * 1.5

        if weight_condition and rsi_condition:
            return {
                "triggered": True,
                "detail": (
                    f"[PORTFOLIO_REBALANCE] {detail} > 1.5×，且 RSI={rsi:.1f} > 65 高位滞涨。"
                    f"当前实际持仓占比远超凯利公式安全建议，数学优势已削弱，"
                    f"提示减仓纠偏，释放现金回流至 CASH。"
                ),
            }

        if weight_condition and not rsi_condition:
            return {
                "triggered": False,
                "detail": (
                    f"{detail} > 1.5× 但 RSI={rsi:.1f} ≤ 65，动量尚可，暂不纠偏"
                ),
            }

        return {"triggered": False, "detail": f"{detail}，偏离未超 1.5× 阈值"}

    def _evaluate_path_d(
        self, pnl_pct: float, current_price: float,
        avg_cost: float, trend_state: str,
        is_cheap: bool = False,
        spy_meltdown: bool = False,
        is_not_cheap: bool = False,
        pe_percentile: float | None = None,
        cash_balance: float = 0.0,
        left_signal_exists: bool = False,
        atr_14: float = 0.0,
    ) -> dict[str, Any]:
        """路径 D：长线价值导向的趋势与止损破位评估。

        核心原则：便宜的核心资产禁止割肉，"输时间不输钱"。

        规则 0（核心资产估值豁免）：PE分位 ≤ 20% → 无视清仓指令，转为防御性卧倒。
        规则 1（估值防御性持有-低PE阴跌+现金补仓雷达）：-15% ≤ PNL < -10% 且 PE分位 ≤ 20% 且有现金 → VALUE_HOLD+补仓提示。
        规则 2（趋势防御性减仓）：仅在 SPY 熔断 OR (PE > 40% 且 bear_core) 时触发减仓 1/3-1/2。
        规则 3（保本垫）：PNL 曾触及 +10% 后回落至成本线 → BREAKEVEN_EXIT。
        """
        pe_str = f"{pe_percentile:.1f}%" if pe_percentile is not None else "N/A"
        trend_is_bear = trend_state == "bear_core"

        # ---- 规则 0：核心资产估值豁免权（PE ≤ 20%） ----
        if is_cheap and trend_is_bear:
            return {
                "triggered": True,
                "tag": "VALUE_HOLD",
                "detail": (
                    f"[VALUE_HOLD] 🟡 趋势虽处于 bear_core，但个股估值已极度便宜"
                    f"（PE分位{pe_str} ≤ 20%），核心资产禁止割肉。"
                    f"建议卧倒不动，静待模块二 L1 信号共振后战略补仓。"
                ),
            }

        # ---- 规则 1：低PE阴跌 + 现金补仓雷达（-15% ≤ PNL < -10%） ----
        if is_cheap and pnl_pct <= -0.10 and pnl_pct >= -0.15 and cash_balance > 0:
            atr_support = current_price - 2.0 * atr_14 if atr_14 > 0 else current_price * 0.90
            trigger_hint = ""
            if left_signal_exists:
                trigger_hint = " | ⚡ 模块二 L1 信号已亮！等待概率轴确认后即刻执行左侧回补"
            return {
                "triggered": True,
                "tag": "VALUE_HOLD",
                "detail": (
                    f"[DEEP_VALUE_ADD] 🟢 当前已有持仓浮亏 {pnl_pct:.1%}，"
                    f"但估值优势巨大（PE分位{pe_str} ≤ 20%），"
                    f"账上现金充足（${cash_balance:,.0f}）。"
                    f"请密切关注下方 ATR 支撑位 ${atr_support:.2f}，"
                    f"等待概率轴扭转后执行左侧回补。{trigger_hint}"
                ),
            }

        # ---- 规则 2：趋势防御性减仓（仅当 SPY 熔断 或 (PE>40% 且 bear_core)） ----
        if trend_is_bear:
            if spy_meltdown:
                return {
                    "triggered": True,
                    "tag": "REDUCE_DEFENSIVE",
                    "detail": (
                        f"[REDUCE_DEFENSIVE] 🔴 大盘系统性风险（SPY 熔断），"
                        f"个股趋势 bear_core（PE分位{pe_str}），"
                        f"建议防御性减仓 1/3 至 1/2，保留子弹等待右侧确认。"
                    ),
                }
            elif is_not_cheap:
                return {
                    "triggered": True,
                    "tag": "REDUCE_DEFENSIVE",
                    "detail": (
                        f"[REDUCE_DEFENSIVE] 🟠 PE分位{pe_str} > 40%（并不便宜），"
                        f"且趋势转向 bear_core。多头结构瓦解，建议减仓 1/3 至 1/2，"
                        f"而非机械清仓。等待估值跌入低价区后再行补仓。"
                    ),
                }
            else:
                # PE 在 20%-40% 之间 + bear_core，但无系统性风险 → 观望
                return {
                    "triggered": False,
                    "detail": (
                        f"趋势 bear_core 但 PE分位{pe_str} 在 20%-40% 中性区间，"
                        f"大盘未熔断，暂不强制减仓。保持现有仓位观望。"
                    ),
                }

        # ---- 规则 3：保本垫（盈利曾触及+10%后回撤至成本线） ----
        breakeven_line = avg_cost * 1.01
        if avg_cost > 0 and current_price < breakeven_line and pnl_pct >= 0.01:
            return {
                "triggered": True,
                "tag": "BREAKEVEN_EXIT",
                "detail": (
                    f"[BREAKEVEN_EXIT] 触发保本硬保护：当前价 ${current_price:.2f} < "
                    f"保本线 ${breakeven_line:.2f}（Avg_Cost × 1.01），"
                    f"浮盈已回撤至成本线，无条件出局，确保本金绝对安全。"
                ),
            }

        # 未触发任何规则
        if trend_is_bear:
            return {
                "triggered": False,
                "detail": (
                    f"趋势 bear_core 但 PE分位{pe_str} 已进入极低价区防御，"
                    f"核心资产豁免生效中。"
                ),
            }
        return {
            "triggered": False,
            "detail": (
                f"价格 ${current_price:.2f}，趋势 '{trend_state}' 未破位，"
                f"PE分位{pe_str}，无出场信号。"
            ),
        }


# ============================================================================
# 报告生成器
# ============================================================================


class RadarReportGenerator:
    """生成综合监控报告。"""

    def __init__(self, gatekeeper: MacroGatekeeper,
                 left_signals: list[dict[str, Any]],
                 right_evaluations: dict[str, dict[str, Any]],
                 washout_results: dict[str, dict[str, Any]],
                 repo: RadarDataRepo,
                 active_holdings: dict[str, dict[str, Any]] | None = None,
                 enriched_holdings: dict[str, dict[str, Any]] | None = None,
                 kelly_results: dict[str, dict[str, Any]] | None = None,
                 module5_results: dict[str, dict[str, Any]] | None = None):
        self.gatekeeper = gatekeeper
        self.left_signals = left_signals
        self.right_evaluations = right_evaluations
        self.washout_results = washout_results
        self.repo = repo
        self.active_holdings = active_holdings or {}
        self.enriched_holdings = enriched_holdings or {}
        self.kelly_results = kelly_results or {}
        self.module5_results = module5_results or {}

    def generate(self) -> str:
        sections = [
            self._header(),
            self._module1_macro(),
            self._module2_left_entry(),
            self._module3_right_addition(),
            self._module4_anti_washout(),
            self._module5_multistage_exit(),
            self._summary(),
            self._monitored_snapshot(),
        ]
        return "\n\n".join(filter(None, sections))

    def _header(self) -> str:
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        status_emoji = "🟢" if self.gatekeeper.is_operational() else "🔴"
        monitor_count = len(self.gatekeeper.monitored_tickers)
        return (
            f"# 🛡️ 量化监控哨所 · 策略雷达报告\n\n"
            f"**生成时间**：{now}  \n"
            f"**监控标的**：{monitor_count} 个（来自 watchlist.csv）  \n"
            f"**系统状态**：{status_emoji} **{self.gatekeeper.system_status}**\n"
        )

    def _module1_macro(self) -> str:
        ms = self.gatekeeper.macro_state
        spy_r = ms.get("spy_regime", "unknown")
        spy_ma20 = ms.get("spy_ma20")
        spy_ma50 = ms.get("spy_ma50")
        spy_ma200 = ms.get("spy_ma200")

        ma_lines = []
        if spy_ma20 and spy_ma50 and spy_ma200:
            alignment = "多头排列 🟢" if spy_ma20 > spy_ma50 > spy_ma200 else (
                "空头排列 🔴" if spy_ma20 < spy_ma50 < spy_ma200 else "纠缠排列 🟡"
            )
            ma_lines.append(
                f"- **SPY 均线排列**：MA20=${spy_ma20:.2f} / MA50=${spy_ma50:.2f} / MA200=${spy_ma200:.2f} → {alignment}"
            )

        monitored_str = ", ".join(self.gatekeeper.monitored_tickers) if self.gatekeeper.monitored_tickers else "无"
        excluded_str = "\n".join(
            f"  - {t}: {r}" for t, r in self.gatekeeper.excluded_tickers.items()
        ) if self.gatekeeper.excluded_tickers else "  无"

        return f"""## 📡 模块一：宏观熔断器与标的筛选

### 大盘状态
- **SPY State**：`{spy_r}` {'✅ 安全' if spy_r in MACRO_BULL_STATES else '⚠️ 异常'}
{chr(10).join(ma_lines)}

### 熔断判定
```
if SPY in ['bull_core', 'neutral_bull']:
    system.status = "OPERATIONAL"  ✅ 安全
else:
    system.status = "MELTDOWN"     🔴 熔断左侧开仓
```
→ 当前状态：**{self.gatekeeper.system_status}**

### 监控标的
**共 {len(self.gatekeeper.monitored_tickers)} 个**：{monitored_str}

**排除清单**：
{excluded_str}
"""

    def _module2_left_entry(self) -> str:
        if not self.left_signals:
            return """## 🎯 模块二：Reverse_Core_Buy_Protocol（左侧黄金建仓）

### 三维极值触发矩阵扫描结果

> ⏳ **当前无标的同时满足三维共振条件。**

**最近各标的状态**：
""" + self._build_monitored_matrix()

        lines = [
            "## 🎯 模块二：Reverse_Core_Buy_Protocol（左侧黄金建仓 Level-1）",
            "",
            f"### 🔥 触发信号！共 {len(self.left_signals)} 个标的满足三维共振条件",
            "",
        ]

        for sig in self.left_signals:
            lines.append(f"#### {sig['ticker']}")
            lines.append(f"")
            lines.append(f"| 维度 | 状态 | 详情 |")
            lines.append(f"|------|------|------|")
            val_icon = "✅" if sig["valuation_ok"] else "❌"
            mom_icon = "✅" if sig["momentum_ok"] else "❌"
            prob_icon = "✅" if sig["probability_ok"] else "❌"
            lines.append(f"| 估值轴（PE≤15%） | {val_icon} | {sig['detail_valuation']} |")
            lines.append(f"| 动能轴（RSI≤33 & Crowding≤5.0） | {mom_icon} | {sig['detail_momentum']} |")
            lines.append(f"| 概率轴（Win_Prob首次扭转>5%） | {prob_icon} | {sig['detail_probability']} |")
            lines.append(f"")

            # 【动态风控输出】
            lines.append(f"### 🛡️ 实战执行风控")
            lines.append(f"| 指标 | 数值 |")
            lines.append(f"|------|------|")
            lines.append(f"| 入场价 | ${sig['entry_price']:.2f} |")
            lines.append(f"| 左侧硬止损（2.0×ATR) | ${sig['left_stop_loss']:.2f} |")
            lines.append(f"| 建议仓位 | {sig['position_size']:.0%}（{sig['position_range']})试探性底仓 |")
            lines.append(f"| ATR(14) | ${sig['atr_14']:.2f} |")
            lines.append(f"")
            lines.append(f"> ⚠️ **严禁一把梭**！此为试探性底仓，等待右侧确认后再补齐主力仓位。")
            lines.append(f">")
            lines.append(f"> 📌 **关键提示**：")
            lines.append(f"> - 入场后如价格触及 **${sig['left_stop_loss']:.2f}**，立即止损离场")
            lines.append(f"> - 等待右侧加仓确认后方能加仓至主力仓位")
            lines.append(f"")

        return "\n".join(lines)

    def _build_monitored_matrix(self) -> str:
        """构建监控标的的三维状态矩阵。"""
        rows = []
        for ticker in sorted(self.gatekeeper.monitored_tickers):
            row = self.repo.get_latest_ticker_metrics(ticker)
            if not row:
                rows.append(f"| {ticker} | N/A | N/A | N/A | N/A | 无数据 |")
                continue

            pe = row.get("PE_Percentile")
            rsi = row.get("RSI")
            crowding = row.get("Crowding_Index")
            wp10 = row.get("Win_Prob_10d")

            pe_str = f"{pe:.1f}%" if pe is not None else "N/A"
            rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
            crowd_str = f"{crowding:.1f}" if crowding is not None else "N/A"
            wp_str = f"{wp10:.1%}" if wp10 is not None else "N/A"

            pe_ok = pe is not None and pe <= PE_PERCENTILE_THRESHOLD

            status_parts = []
            # 使用动态阈值（以 repo.get_percentile_thresholds 为准）
            pct = self.repo.get_percentile_thresholds(ticker)
            rsi_th = pct["rsi_p15"]
            crowd_th = pct["crowding_p15"]
            rsi_ok = rsi is not None and rsi <= rsi_th
            crowd_ok = crowding is not None and crowding <= crowd_th
            status_parts.append("PE✅" if pe_ok else "PE✗")
            status_parts.append(f"RSI≤{rsi_th:.0f}" if rsi_ok else f"RSI>{rsi_th:.0f}")
            status_parts.append(f"CR≤{crowd_th:.0f}" if crowd_ok else f"CR>{crowd_th:.0f}")
            status = " | ".join(status_parts)

            rows.append(f"| {ticker} | {pe_str} | {rsi_str} | {crowd_str} | {wp_str} | {status} |")

        header = "| Ticker | PE分位 | RSI(14) | Crowding | Win_Prob_10d | 估值/动能状态 |\n"
        header += "|--------|--------|---------|----------|-------------|--------------|\n"
        return header + "\n".join(rows)

    def _module3_right_addition(self) -> str:
        if not self.right_evaluations:
            return ""

        lines = [
            "## 📈 模块三：右侧加仓与风险追踪",
            "",
        ]

        for ticker, eval_result in self.right_evaluations.items():
            ready_icon = "🟢" if eval_result.get("ready") else "🟡"
            is_holding = eval_result.get("is_holding", False)
            label = "🔷 持仓追踪" if is_holding else f"{ready_icon}"
            lines.append(f"### {ticker} {label}")

            if is_holding:
                lines.append(f"#### 当前持仓信息")
                avg_cost = eval_result.get('avg_cost', 0)
                total_qty = eval_result.get('total_qty', 0)
                lines.append(f"- **移动成本价**：${avg_cost:.2f}")
                lines.append(f"- **持仓数量**：{total_qty:.0f} 股")

                # 【主力风控追踪】
                main_stop_loss = eval_result.get('main_stop_loss')
                if main_stop_loss:
                    atr = eval_result.get('atr_14', 0)
                    lines.append(f"")
                    lines.append(f"#### 🛡️ 主力风控追踪（移动止损）")
                    lines.append(f"- **主力止损线（1.5×ATR）**：${main_stop_loss:.2f}")
                    if atr:
                        lines.append(f"- **ATR(14)**：${atr:.2f}")
                    lines.append(f"- **触发止损**：如价格跌破 ${main_stop_loss:.2f}，立即止损")

            lines.append(f"")
            lines.append(f"#### 加仓评估")
            lines.append(f"- **加仓就绪**：{'✅ 是' if eval_result.get('ready') else '⏳ 否'}")
            lines.append(f"- **原因**：{eval_result.get('reason', 'N/A')}")
            lines.append(f"- **IV状态**：{eval_result.get('iv_status', 'N/A')}")
            lines.append(f"- **趋势状态**：{eval_result.get('trend_state', 'N/A')}")
            lines.append(f"")

        return "\n".join(lines)

    def _module4_anti_washout(self) -> str:
        if not self.washout_results:
            return ""

        # 过滤有激活结果的
        active = {t: r for t, r in self.washout_results.items() if r.get("activated")}
        if not active:
            return (
                "## 🔄 模块四：防洗飞与右侧回补协议\n\n"
                "> ⏳ 当前无标的激活防洗飞协议。\n"
            )

        lines = [
            "## 🔄 模块四：防洗飞与右侧回补协议（Anti-Washout）",
            "",
        ]

        for ticker, result in active.items():
            lines.append(f"### {ticker}")
            lines.append(f"- **Entry_Ref（清仓离场价）**：${result.get('entry_ref', 'N/A')}")
            lines.append(f"- **距止损天数**：{result.get('days_since_stop', 'N/A')} 天")
            lines.append(f"")
            lines.append(f"#### 路径 A（动能修复）{'✅' if result.get('path_a_triggered') else '⏳'}")
            lines.append(f"{result.get('path_a_detail', 'N/A')}")
            lines.append(f"")
            lines.append(f"#### 路径 B（指标共振）{'✅' if result.get('path_b_triggered') else '⏳'}")
            lines.append(f"{result.get('path_b_detail', 'N/A')}")
            lines.append(f"")
            lines.append(f"**综合建议**：{result.get('recommendation', 'N/A')}")
            lines.append(f"")

        return "\n".join(lines)

    def _module5_multistage_exit(self) -> str:
        """模块五：多维阶梯式出场与动态资金管理协议报告。"""
        if not self.enriched_holdings:
            return ""

        # 计算总体指标
        total_cash = 0.0
        total_nav = 0.0
        for enriched in self.enriched_holdings.values():
            total_cash = enriched.get("cash_balance", 0)
            total_nav = enriched.get("total_nav", 0)
            break  # 所有 enriched 的 cash_balance / total_nav 相同

        lines = [
            "## 🔶 模块五：持仓动态出场与资金管理追踪",
            "",
            f"### 账户总览",
            f"- **总资产 (NAV)**：${total_nav:,.2f}",
            f"- **闲置现金 (CASH)**：${total_cash:,.2f}",
            f"- **持仓标的数**：{len(self.enriched_holdings)} 个",
            f"",
        ]

        # 资金管理概要：凯利权重表
        lines.append("### 凯利公式资金管理")
        lines.append("")
        lines.append("| Ticker | RR_Score(b) | 动态胜率(p) | 全仓凯利 | 半仓凯利 | 最终目标 | 当前实际占比 |")
        lines.append("|--------|------------|------------|---------|---------|---------|------------|")
        for ticker, enriched in self.enriched_holdings.items():
            kelly = self.kelly_results.get(ticker, {})
            actual_w = enriched.get("actual_weight", 0)
            lines.append(
                f"| {ticker} | {kelly.get('b', 0):.1f} | {kelly.get('p', 0):.0%} | "
                f"{kelly.get('target_weight', 0):.1%} | {kelly.get('suggested_weight', 0):.1%} | "
                f"{kelly.get('final_target_weight', 0):.1%} | {actual_w:.1%} |"
            )
        lines.append("")

        # 逐个持仓的阶梯式出场评估
        lines.append("### 阶梯式出场评估")
        lines.append("")

        triggered_count = 0
        for ticker, enriched in self.enriched_holdings.items():
            m5 = self.module5_results.get(ticker, {})
            pnl = enriched.get("pnl_pct", 0)
            current_price = enriched.get("current_price", 0)
            avg_cost = enriched.get("avg_cost", 0)
            actual_w = enriched.get("actual_weight", 0)

            lines.append(f"#### {ticker}")
            lines.append(f"- **持仓成本**：${avg_cost:.2f}  |  **当前价**：${current_price:.2f}  |  **浮盈**：{pnl:.1%}  |  **实际占比**：{actual_w:.1%}")
            lines.append(f"")

            if m5.get("triggered"):
                triggered_count += 1
                lines.append(f"##### ⚠️ 触发信号！路径：{', '.join(m5.get('signals', []))} | 行动标签：`{m5.get('primary_action_tag', '')}`")
                lines.append(f"")
                lines.append(f"> {m5.get('primary_action', '')}")
            else:
                lines.append(f"✅ 持仓正常，未触发出场信号")

            # 展开四路径详情
            lines.append(f"")
            for path_key in ("path_a", "path_b", "path_c", "path_d"):
                path_data = m5.get(path_key)
                if path_data:
                    status = "🔴 触发" if path_data.get("triggered") else "🟢 通过"
                    path_label = {"path_a": "路径A (盈亏比恶化)", "path_b": "路径B (动态RSI)", "path_c": "路径C (凯利纠偏)", "path_d": "路径D (硬保护)"}.get(path_key, path_key)
                    lines.append(f"- **{path_label}** {status}：{path_data.get('detail', '')}")
            lines.append(f"")

        if triggered_count == 0:
            lines.insert(3, "> ⚪ 所有持仓标的暂未触发出场信号，风控状态正常。\n")
        else:
            lines.insert(3, f"> 🔴 {triggered_count} 个标的触发出场信号，请优先处理！\n")

        return "\n".join(lines)

    def _summary(self) -> str:
        actionable = []

        if self.gatekeeper.system_status == "MELTDOWN":
            actionable.append("🔴 **系统熔断**：大盘不满足安全条件，拒绝执行任何个股 Reverse_Core_Buy_Protocol。建议持有现金观望。")

        if self.left_signals:
            names = [s["ticker"] for s in self.left_signals]
            actionable.append(f"🟢 **左侧建仓信号**：{', '.join(names)} 触发三维共振，可按 {LEFT_ENTRY_POSITION_MIN:.0%}-{LEFT_ENTRY_POSITION_MAX:.0%} 仓位试探建仓。")

        # 收集路径A/B触发的洗飞回补
        washout_triggered = []
        for t, r in self.washout_results.items():
            if r.get("activated") and (r.get("path_a_triggered") or r.get("path_b_triggered")):
                washout_triggered.append(t)
        if washout_triggered:
            actionable.append(f"🔄 **防洗飞回补**：{', '.join(washout_triggered)} 满足回补条件，可考虑重新建仓。")

        if not actionable:
            actionable.append("⏳ **当前无操作信号**：继续监控 watchlist 标的，等待三维共振或回补信号。")

        return "## 📋 综合决策摘要\n\n" + "\n".join(f"- {a}" for a in actionable)

    def _generate_snapshot_advice(self, ticker: str, row: dict[str, Any]) -> str:
        """根据持仓状态和三维指标状态生成细化建议。

        优先级：
        1. 如果是已有持仓标的 → 优先对齐模块五的结论
        2. 如果无持仓 → 套用模块二的三维矩阵逻辑
        """
        # 【优先级1】检查是否是已有持仓标的 → 模块五优先
        if ticker in self.active_holdings:
            m5 = self.module5_results.get(ticker, {})
            enriched = self.enriched_holdings.get(ticker, {})
            pnl = enriched.get("pnl_pct", 0) if enriched else 0
            actual_w = enriched.get("actual_weight", 0) if enriched else 0

            if m5.get("triggered"):
                tag = m5.get("primary_action_tag", "")
                tag_icon = {"TAKE_PROFIT_ALL": "🔴", "REDUCE_POSITION": "🟠", "PORTFOLIO_REBALANCE": "🟡", "BREAKEVEN_EXIT": "🔴", "TREND_EXIT": "🔴", "REDUCE_DEFENSIVE": "🟠", "VALUE_HOLD": "🟢", "DEEP_VALUE_ADD": "🟢"}.get(tag, "🔵")
                return f"{tag_icon} 持仓追踪中：[{tag}] {m5.get('primary_action', '')}"
            else:
                return f"🔵 持仓追踪中：浮盈{pnl:.1%} | 占比{actual_w:.1%} | 无出場信号"

        # 【优先级2】无持仓标的 → 套用三维矩阵逻辑
        pe = row.get("PE_Percentile")
        rsi = row.get("RSI")
        crowding = row.get("Crowding_Index")
        win_prob = row.get("Win_Prob_10d")

        # 检查三个极值条件
        pe_ok = pe is not None and pe <= PE_PERCENTILE_THRESHOLD
        rsi_ok = rsi is not None and rsi <= RSI_THRESHOLD
        crowding_ok = crowding is not None and crowding <= CROWDING_THRESHOLD

        # 计算满足的条件数
        conditions_met = sum([pe_ok, rsi_ok, crowding_ok])

        # 检查概率轴（简化版：只看当前值 > 5%）
        prob_ok = win_prob is not None and win_prob > WIN_PROB_FLIP_THRESHOLD / 100.0

        # 【情况1】三维完美共振
        if pe_ok and rsi_ok and crowding_ok and prob_ok:
            close = row.get("Close_Price", 0)
            atr = row.get("ATR_14", 0) or 0
            stop_loss = close - LEFT_ATR_MULTIPLIER * atr if atr > 0 else close * 0.90
            return (
                f"🟢 Level-1 黄金建仓：PE{pe:.1f}% + RSI{rsi:.0f} + CR{crowding:.1f}，"
                f"建议分批介入 10%-15% 底仓，硬止损锁定 ${stop_loss:.2f}"
            )

        # 【情况2】满足 1-2 个极值条件，但未完美共振
        if conditions_met >= 1 and conditions_met <= 2:
            triggered = []
            if pe_ok:
                triggered.append(f"PE{pe:.1f}%")
            if rsi_ok:
                triggered.append(f"RSI{rsi:.0f}")
            if crowding_ok:
                triggered.append(f"CR{crowding:.1f}")
            condition_str = " + ".join(triggered)
            return (
                f"🟡 左侧蓄势/密切监控：{condition_str} 极值已现，等待概率轴扭转共振"
            )

        # 【情况3】完全没有触发任何极值条件
        if conditions_met == 0:
            reason_parts = []
            if pe is not None:
                reason_parts.append(f"PE{pe:.1f}%")
            if rsi is not None:
                reason_parts.append(f"RSI{rsi:.0f}")
            if crowding is not None:
                reason_parts.append(f"CR{crowding:.1f}")
            reason_str = " / ".join(reason_parts) if reason_parts else "全无极值"
            return f"🔴 暂无信号：{reason_str} 均未触发，耐心观望"

        # 其他情况（不应该出现）
        return "⚪ 待评估"

    def _monitored_snapshot(self) -> str:
        """监控标的快照表格。"""
        rows = []
        for ticker in sorted(self.gatekeeper.monitored_tickers):
            row = self.repo.get_latest_ticker_metrics(ticker)
            if not row:
                continue
            date_str = str(row.get("Date", ""))[:10]
            close = row.get("Close_Price", 0)
            trend = row.get("Trend_State", "N/A")
            rsi = row.get("RSI")
            rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
            atr = row.get("ATR_14", 0)
            atr_str = f"${atr:.2f}" if atr else "N/A"
            wp10 = row.get("Win_Prob_10d")
            wp_str = f"{wp10:.1%}" if wp10 is not None else "N/A"
            pe = row.get("PE_Percentile")
            pe_str = f"{pe:.1f}%" if pe is not None else "N/A"
            iv = row.get("IV_Status", "N/A")

            # 【新增】使用细化建议逻辑代替硬编码的 action
            advice = self._generate_snapshot_advice(ticker, row)

            rows.append(
                f"| {date_str} | {ticker} | ${close:.2f} | {trend} | {rsi_str} | "
                f"{atr_str} | {pe_str} | {iv} | {wp_str} | {advice} |"
            )

        header = (
            "| 日期 | Ticker | 价格 | 趋势 | RSI | ATR | PE分位 | IV | Win_Prob_10d | 建议 |\n"
            "|------|--------|------|------|-----|-----|--------|----|-------------|------|\n"
        )
        return "## 📊 监控标的快照\n\n" + header + "\n".join(rows)


# ============================================================================
# 主程序
# ============================================================================


def run_radar(repo: RadarDataRepo, watchlist: list[str] | None = None,
            verbose: bool = False, force_bull: bool = False) -> str:
    """运行完整的策略雷达扫描（五模块）。"""
    if watchlist is None:
        watchlist = _read_watchlist_symbols()

    # 读取持仓记录
    holdings_records = _read_holdings_csv()
    active_holdings = _get_active_holdings(holdings_records)

    # ---- 模块一：宏观熔断器 ----
    gatekeeper = MacroGatekeeper(repo)
    macro_result = gatekeeper.evaluate(watchlist)

    if force_bull:
        gatekeeper.system_status = "OPERATIONAL"
        macro_result["system_status"] = "OPERATIONAL"

    if verbose:
        print(f"[模块一] 大盘状态: SPY={macro_result['spy_regime']} → {gatekeeper.system_status}")
        print(f"[模块一] 监控标的 ({len(gatekeeper.monitored_tickers)} 个): {gatekeeper.monitored_tickers}")
        if active_holdings:
            print(f"[持仓] 当前持有: {list(active_holdings.keys())}")

    # ---- 模块二：左侧建仓扫描 ----
    left_protocol = ReverseCoreBuyProtocol(repo)
    left_signals: list[dict[str, Any]] = []

    if gatekeeper.is_operational() and gatekeeper.monitored_tickers:
        left_signals = left_protocol.scan_all(gatekeeper.monitored_tickers)
        if verbose:
            print(f"[模块二] 左侧信号: {len(left_signals)} 个标的触发")

    # ---- 模块三：右侧加仓与持仓追踪 ----
    right_evaluations: dict[str, dict[str, Any]] = {}

    for sig in left_signals:
        ticker = sig["ticker"]
        eval_result = RightSideAddition.evaluate_addition(
            ticker, repo,
            left_entry_price=sig.get("entry_price", 0),
            left_stop_loss=sig.get("left_stop_loss", 0),
        )
        right_evaluations[ticker] = eval_result

    if gatekeeper.is_operational() and active_holdings:
        for ticker, pos in active_holdings.items():
            if ticker in right_evaluations:
                continue
            avg_cost = pos.get("avg_cost", 0)
            row = repo.get_latest_ticker_metrics(ticker)
            atr = row.get("ATR_14", 0) or 0 if row else 0
            stop_loss = avg_cost - RIGHT_ATR_MULTIPLIER * atr if atr > 0 and avg_cost > 0 else avg_cost * 0.90
            eval_result = RightSideAddition.evaluate_addition(
                ticker, repo,
                left_entry_price=avg_cost,
                left_stop_loss=stop_loss,
            )
            eval_result["is_holding"] = True
            eval_result["avg_cost"] = avg_cost
            eval_result["total_qty"] = pos.get("total_qty", 0)
            right_evaluations[ticker] = eval_result
        if verbose and active_holdings:
            h_eval = [t for t in active_holdings if t in right_evaluations]
            print(f"[模块三] 持仓追踪: {len(h_eval)} 个标的已评估")

    # ---- 模块四：防洗飞协议 ----
    washout_protocol = AntiWashoutProtocol(repo)
    washout_results: dict[str, dict[str, Any]] = {}

    if gatekeeper.is_operational():
        for ticker in gatekeeper.monitored_tickers:
            row = repo.get_latest_ticker_metrics(ticker)
            latest_date = str(row.get("Date", ""))[:10] if row else ""
            last_stop = _get_last_stop_out_30d(holdings_records, ticker, latest_date)
            washout_result = washout_protocol.evaluate(
                ticker, gatekeeper.system_status, last_stop_out=last_stop
            )
            if washout_result.get("activated"):
                washout_results[ticker] = washout_result

    # ---- 模块五：多维阶梯式出场与动态资金管理 ----
    # 步骤1：用最新市场数据丰富持仓信息
    enriched_holdings = _enrich_holdings_with_market_data(active_holdings, repo)

    # 步骤2：计算每只持仓标的的凯利权重
    kelly_results: dict[str, dict[str, Any]] = {}
    for ticker in active_holdings:
        row = repo.get_latest_ticker_metrics(ticker)
        if not row:
            continue
        washout_triggered = ticker in washout_results and washout_results[ticker].get("activated", False)
        kelly = _calculate_kelly_weight(
            ticker, row, macro_result, washout_triggered=washout_triggered
        )
        kelly_results[ticker] = kelly

    # 步骤3：对每个持仓标的执行四路径阶梯式出场评估
    m5_protocol = MultiStageExitProtocol()
    module5_results: dict[str, dict[str, Any]] = {}

    # 构建左侧信号标的集合（用于 DEEP_VALUE_ADD 上下文联动）
    left_signal_tickers = {sig["ticker"] for sig in left_signals} if left_signals else set()

    # 获取全局现金余额
    global_cash = 0.0
    for enriched in enriched_holdings.values():
        global_cash = enriched.get("cash_balance", 0)
        break

    for ticker in active_holdings:
        enriched = enriched_holdings.get(ticker)
        row = repo.get_latest_ticker_metrics(ticker)
        kelly = kelly_results.get(ticker, {})
        if not enriched or not row:
            continue
        pe_percentile = row.get("PE_Percentile")
        left_signal_exists = ticker in left_signal_tickers
        m5_result = m5_protocol.evaluate(
            ticker, enriched, row, kelly, gatekeeper.system_status,
            pe_percentile=pe_percentile,
            cash_balance=global_cash,
            left_signal_exists=left_signal_exists,
        )
        module5_results[ticker] = m5_result

    if verbose and active_holdings:
        triggered = [t for t, r in module5_results.items() if r.get("triggered")]
        print(f"[模块五] 出场信号: {len(triggered)} 个标的触发 ({', '.join(triggered) if triggered else '无'})")

    # ---- 生成报告 ----
    generator = RadarReportGenerator(
        gatekeeper, left_signals, right_evaluations, washout_results, repo,
        active_holdings=active_holdings,
        enriched_holdings=enriched_holdings,
        kelly_results=kelly_results,
        module5_results=module5_results,
    )
    report = generator.generate()

    return report


def main():
    import os
    os.environ['PYTHONIOENCODING'] = 'utf-8'

    parser = argparse.ArgumentParser(
        description="strategy_radar — four-module quantitative monitoring outpost"
    )
    parser.add_argument("--db", type=str, default=str(DB_PATH),
                        help="SQLite 数据库路径")
    parser.add_argument("--tickers", type=str, default=None,
                        help="手动指定 ticker 列表（逗号分隔），默认从 watchlist.csv 读取")
    parser.add_argument("--as-of", type=str, default=None, dest="as_of_date",
                        help="回测日期节点 YYYY-MM-DD，仅使用 <= 该日期的数据进行扫描")
    parser.add_argument("--force-bull", action="store_true", dest="force_bull",
                        help="回测模式下强制将大盘状态设为 OPERATIONAL")
    parser.add_argument("--output", type=str, default=None,
                        help="输出报告路径（默认自动生成到 output/ 目录）")
    parser.add_argument("--print", action="store_true", dest="print_report",
                        help="在控制台打印完整报告")
    parser.add_argument("--verbose", action="store_true",
                        help="打印详细处理过程")

    args = parser.parse_args()

    db_path = pathlib.Path(args.db)
    if not db_path.is_file():
        print(f"[ERR] 数据库不存在: {db_path}")
        return

    # 回测日期校验
    as_of_date = args.as_of_date
    if as_of_date:
        try:
            datetime.strptime(as_of_date, "%Y-%m-%d")
        except ValueError:
            print(f"[ERR] --as-of 日期格式无效: {as_of_date}，请使用 YYYY-MM-DD")
            return

    # 确定 ticker 列表：默认从 watchlist.csv 读取
    if args.tickers:
        watchlist = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        watchlist = _read_watchlist_symbols()

    if not watchlist:
        print("[ERR] 未获取到任何 ticker，请检查 watchlist.csv 或使用 --tickers 手动指定")
        return

    repo = RadarDataRepo(db_path, as_of_date=as_of_date)

    force_bull = args.force_bull and as_of_date is not None

    print("=" * 60)
    print("  Strategy Radar - Five-Module Quantitative Monitoring")
    print(f"  Data source: ticker_metrics table ({len(watchlist)} tickers)")
    if as_of_date:
        print(f"  回测模式：仅使用 <= {as_of_date} 的数据")
    if force_bull:
        print("  强制牛市：宏观熔断器已绕过")
    print("=" * 60)
    print()

    report = run_radar(repo, watchlist=watchlist, verbose=args.verbose,
                       force_bull=force_bull)

    # 确定输出路径
    if args.output:
        output_path = pathlib.Path(args.output)
    else:
        if as_of_date:
            date_tag = as_of_date.replace("-", "")
            if force_bull:
                date_tag = f"asof_{date_tag}_bull"
            else:
                date_tag = f"asof_{date_tag}"
        else:
            date_tag = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M")
        output_path = OUTPUT_DIR / f"strategy_radar_{date_tag}.md"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"[OK] 报告已保存至: {output_path}")

    if args.print_report:
        print("\n" + report)

    print()
    print("=" * 60)
    print("  扫描完成。")
    print("=" * 60)


if __name__ == "__main__":
    main()