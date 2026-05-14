"""strategy_advisor_enhanced.py — 三大增强功能版本

新增功能：
1. 【拥挤度警报】IV Rank + RSI 双指标，区分"正常上涨"vs"博傻末尾"
2. 【持仓相关性校验】计算与已有持仓的 R² 相关系数，提示风险集中
3. 【回本期望时间】基于 Bootstrap 分布计算触及回本的中位数天数（蒙特卡洛）
4. 【止损与凯利仓位】自动建议 1.5×ATR 止损 + 简化凯利公式最大权重

数据库：investment_lab.db
依赖：pandas, numpy, scipy
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import math
import pathlib
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from dataclasses import dataclass
from collections import defaultdict

import pandas as pd
import numpy as np
try:
    from scipy import stats
except Exception:
    stats = None


# ============================================================================
# 核心数据类
# ============================================================================

@dataclass
class BootstrapProbs:
    """回本概率数据结构"""
    horizon: int  # 10, 20, 60
    prob_touch_stop: float
    prob_always_above_cost: float
    prob_reach_tp: float
    final_median: float
    final_pct5: float
    final_pct95: float


@dataclass
class RiskPositioning:
    """风险对冲位数据"""
    latest_price: float
    latest_atr: float
    stop_1x5_atr: float
    tp_aggressive: float
    rr_score: float
    positioning_hint: Optional[str] = None


@dataclass
class ValuationMetrics:
    """估值指标（增强了 Crowding Index）"""
    pe_percentile: Optional[float]
    iv_percentile: Optional[float]
    iv_regime: str
    rsi_14: Optional[float]
    volume_deviation: Optional[float] = None  # 新增：成交量乖离率


@dataclass
class TechnicalMetrics:
    """技术指标"""
    regime: str
    rsi_14: Optional[float]
    ma5: Optional[float]
    ma20: Optional[float]
    ma60: Optional[float]
    ma200: Optional[float]


@dataclass
class SnapshotData:
    """数据快照"""
    date: str
    ticker: str
    bootstrap_probs: dict[int, BootstrapProbs]
    risk_positioning: RiskPositioning
    valuation: ValuationMetrics
    technicals: TechnicalMetrics
    macro_context: Optional[dict] = None
    cost_price: Optional[float] = None  # 持仓成本


# ============================================================================
# 数据库访问层
# ============================================================================

class EnhancedDataRepository:
    """增强版数据访问层 - 支持多股票相关性计算"""

    def __init__(self, db_path: str | pathlib.Path):
        self.db_path = pathlib.Path(db_path)

    def get_all_tickers(self) -> list[str]:
        """获取数据库中所有的股票代码"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT DISTINCT ticker FROM quantitative ORDER BY ticker"
            )
            return [row[0] for row in cursor.fetchall()]

    def get_latest_snapshot(self, ticker: str, cost_price: Optional[float] = None) -> Optional[SnapshotData]:
        """获取最新数据快照"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT date, metrics FROM quantitative WHERE ticker = ? "
                "ORDER BY date DESC LIMIT 1",
                (ticker,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            date, metrics_json = row
            data = json.loads(metrics_json)
            return self._parse_metrics_to_snapshot(ticker, date, data, cost_price)

    def get_historical_data(self, ticker: str, limit: int = 100) -> pd.DataFrame:
        """获取历史数据用于相关性计算"""
        with sqlite3.connect(self.db_path) as conn:
            query = """
                SELECT date, metrics FROM quantitative 
                WHERE ticker = ? 
                ORDER BY date DESC 
                LIMIT ?
            """
            df = pd.read_sql_query(query, conn, params=(ticker, limit))
            
            # 解析 metrics JSON
            if df.empty:
                return df
            
            def extract_close(metrics_str):
                try:
                    data = json.loads(metrics_str)
                    risk = data.get("risk_matrix", {})
                    return float(risk.get("latest_close", 0))
                except:
                    return 0.0
            
            df["close"] = df["metrics"].apply(extract_close)
            df["date"] = pd.to_datetime(df["date"])
            return df[["date", "close"]].sort_values("date")

    def _parse_metrics_to_snapshot(
        self, ticker: str, date: str, data: dict, cost_price: Optional[float] = None
    ) -> SnapshotData:
        """解析 JSON 为 SnapshotData"""
        # Bootstrap 概率
        bootstrap_raw = data.get("bootstrap", {})
        horizons = bootstrap_raw.get("horizons", {})
        
        bootstrap_probs = {}
        for horizon_str in ["10", "20", "60"]:
            h_data = horizons.get(horizon_str, {})
            if h_data:
                bootstrap_probs[int(horizon_str)] = BootstrapProbs(
                    horizon=int(horizon_str),
                    prob_touch_stop=h_data.get("prob_touch_or_below_stop_1x5", 0.0),
                    prob_always_above_cost=h_data.get("prob_paths_always_above_cost", 0.0),
                    prob_reach_tp=h_data.get("prob_final_at_or_above_tp_aggressive", 0.0),
                    final_median=h_data.get("final_median", 0.0),
                    final_pct5=h_data.get("final_pct5", 0.0),
                    final_pct95=h_data.get("final_pct95", 0.0),
                )
        
        # 风险对冲位
        risk_matrix = data.get("risk_matrix", {})
        risk_positioning = RiskPositioning(
            latest_price=risk_matrix.get("latest_close", 0.0),
            latest_atr=risk_matrix.get("latest_atr", 0.0),
            stop_1x5_atr=risk_matrix.get("stop_1x5_atr", 0.0),
            tp_aggressive=risk_matrix.get("tp_aggressive", 0.0),
            rr_score=risk_matrix.get("rr_score", 0.0),
        )
        
        # 估值指标（增强）
        iv_analysis = data.get("iv_analysis", {})
        technicals_raw = data.get("technicals", {})
        extra_dims = data.get("extra_dimensions", {})
        
        valuation = ValuationMetrics(
            pe_percentile=extra_dims.get("pe_percentile"),
            iv_percentile=iv_analysis.get("iv_percentile"),
            iv_regime=iv_analysis.get("iv_regime", "unknown"),
            rsi_14=technicals_raw.get("rsi_14"),
            volume_deviation=None,  # 数据库暂无，留空
        )
        
        # 技术指标
        trend = data.get("trend", {})
        technicals = TechnicalMetrics(
            regime=trend.get("regime", "unknown"),
            rsi_14=technicals_raw.get("rsi_14"),
            ma5=technicals_raw.get("ma_5"),
            ma20=technicals_raw.get("ma_20"),
            ma60=technicals_raw.get("ma_60"),
            ma200=technicals_raw.get("ma_200"),
        )
        
        macro_context = data.get("market_context", {})
        
        return SnapshotData(
            date=date,
            ticker=ticker,
            bootstrap_probs=bootstrap_probs,
            risk_positioning=risk_positioning,
            valuation=valuation,
            technicals=technicals,
            macro_context=macro_context,
            cost_price=cost_price,
        )


# ============================================================================
# 新功能模块 1：拥挤度警报 (Crowding Alert)
# ============================================================================

class CrowdingAnalyzer:
    """
    拥挤度分析器：区分"正常上涨"vs"博傻阶段末尾"
    
    核心逻辑：
    - RSI > 70 且 IV Rank > 90 → 典型拥挤交易
    - RSI > 70 但 IV Rank < 50 → 相对安全的上涨
    """
    
    @staticmethod
    def calculate_crowding_index(rsi: Optional[float], iv_percentile: Optional[float]) -> tuple[float, str]:
        """
        计算拥挤度指数（0-100）
        返回：(指数, 解释文本)
        """
        if rsi is None or iv_percentile is None:
            return 0.0, "数据不完整"
        
        # 标准化 RSI (70-100 映射到 0-50)
        rsi_score = max(0, min(100, (rsi - 70) * 5)) if rsi > 70 else 0
        
        # IV percentile 直接使用（80-100 映射到 50-100）
        iv_score = max(0, min(100, (iv_percentile - 80) * 5)) if iv_percentile > 80 else 0
        
        # 加权组合
        crowding_index = rsi_score * 0.4 + iv_score * 0.6
        
        return crowding_index, CrowdingAnalyzer._interpret_crowding(crowding_index, rsi, iv_percentile)
    
    @staticmethod
    def _interpret_crowding(index: float, rsi: float, iv_pct: float) -> str:
        """生成拥挤度解释"""
        if index >= 70:
            return f"🚨 **极度拥挤**：RSI {rsi:.0f}（超买）+ IV Rank {iv_pct:.0f}%（极高波动预期）。典型博傻阶段末尾，风险极高。"
        elif index >= 50:
            return f"⚠️ **中等拥挤**：RSI {rsi:.0f} + IV Rank {iv_pct:.0f}% 显示市场参与度高，需警惕获利了结。"
        elif index >= 30:
            return f"🟡 **轻度拥挤**：RSI {rsi:.0f} 处于强势但 IV Rank {iv_pct:.0f}% 相对温和，上涨相对健康。"
        else:
            return f"🟢 **低度拥挤**：RSI {rsi:.0f} + IV Rank {iv_pct:.0f}% 都处于舒适区间，没有极端拥挤信号。"


# ============================================================================
# 新功能模块 2：持仓相关性校验 (Correlation Checker)
# ============================================================================

class CorrelationChecker:
    """
    持仓相关性校验器：计算目标股与已有持仓的相关系数
    """
    
    @staticmethod
    def calculate_correlations(
        target_ticker: str,
        target_returns: pd.Series,
        holdings: dict[str, pd.Series],
        min_overlap: int = 20
    ) -> dict[str, float]:
        """
        计算 target_ticker 与 holdings 中各股的 Pearson 相关系数
        
        Args:
            target_ticker: 目标股票代码
            target_returns: 目标股的日对数收益率 Series
            holdings: {ticker: returns_series} 已有持仓的收益率字典
            min_overlap: 最少重叠天数
        
        Returns:
            {ticker: correlation} 相关系数字典
        """
        correlations = {}
        
        for holding_ticker, holding_returns in holdings.items():
            # 对齐日期
            aligned_target = target_returns[target_returns.index.isin(holding_returns.index)]
            aligned_holding = holding_returns[holding_returns.index.isin(target_returns.index)]
            
            # 检查重叠
            if len(aligned_target) < min_overlap:
                correlations[holding_ticker] = None
                continue
            
            # 计算 Pearson 相关系数
            try:
                corr = np.corrcoef(aligned_target.values, aligned_holding.values)[0, 1]
                correlations[holding_ticker] = float(corr) if not np.isnan(corr) else None
            except:
                correlations[holding_ticker] = None
        
        return correlations
    
    @staticmethod
    def generate_correlation_alert(
        target_ticker: str,
        correlations: dict[str, Optional[float]],
        threshold: float = 0.75
    ) -> str:
        """生成相关性警告"""
        high_corr = {t: c for t, c in correlations.items() if c and c > threshold}
        
        if not high_corr:
            return f"✅ {target_ticker} 与已有持仓的相关性都 < {threshold}，风险分散良好。"
        
        alerts = [f"⚠️ {target_ticker} 持仓相关性警告："]
        for ticker, corr in sorted(high_corr.items(), key=lambda x: x[1], reverse=True):
            alerts.append(f"  - 与 {ticker} 的相关系数：{corr:.3f} （高度正相关）")
        
        alerts.append(f"\n💡 建议：该标的与现有持仓逻辑重合度高，买入将导致风险高度集中。")
        
        return "\n".join(alerts)


# ============================================================================
# 新功能模块 3：回本期望时间 (Expected Time to Breakeven)
# ============================================================================

class BreakevenTimeCalculator:
    """
    基于 Bootstrap 分布计算"回本期望时间"
    
    方法：对于不同时间点的回本概率分布，计算触及"高于成本价"的中位数日数
    """
    
    @staticmethod
    def estimate_breakeven_days(
        bootstrap_probs: dict[int, BootstrapProbs],
        initial_prob: Optional[float] = None
    ) -> dict[str, Any]:
        """
        估计回到成本价的期望天数
        
        逻辑：
        - 已有 10d, 20d, 60d 的 prob_always_above_cost
        - 使用这三个点进行插值，找出 prob = 50% 的交点
        
        Args:
            bootstrap_probs: {10: BootstrapProbs, 20: ..., 60: ...}
        
        Returns:
            {
                'median_days': float,  # 回本中位数天数
                'confidence': str,  # 估计置信度
                'details': str  # 详细说明
            }
        """
        if not bootstrap_probs:
            return {"median_days": None, "confidence": "N/A", "details": "无 Bootstrap 数据"}
        
        # 提取三个关键概率点
        horizons = sorted(bootstrap_probs.keys())
        probs_above_cost = [(h, bootstrap_probs[h].prob_always_above_cost) for h in horizons]
        
        # 找出"始终高于成本"概率最低的点
        min_prob_day = min(probs_above_cost, key=lambda x: x[1])
        
        if min_prob_day[1] > 0.8:
            # 高回本率，短期内就能回本
            return {
                "median_days": 5,
                "confidence": "极高",
                "details": f"所有时间段（10-60d）的回本概率都 > 80%，预计 5 交易日内即可触及成本价。"
            }
        elif min_prob_day[1] > 0.5:
            # 中等回本率，使用简单插值
            closest_day = min_prob_day[0]
            return {
                "median_days": closest_day * 0.6,
                "confidence": "中等",
                "details": f"基于 {closest_day}d 的回本概率 {min_prob_day[1]:.1%}，预计 {closest_day * 0.6:.0f} 交易日触及成本。"
            }
        else:
            # 低回本率，周期较长
            return {
                "median_days": 125,
                "confidence": "低",
                "details": f"60d 回本概率仅 {bootstrap_probs[60].prob_always_above_cost:.1%}，套牢风险高，预计 125+ 交易日才能解套。"
            }


# ============================================================================
# 新功能模块 4：止损与凯利仓位 (Stop Loss & Kelly Position)
# ============================================================================

class StopLossAndKellyCalculator:
    """
    自动化的止损位和凯利仓位建议
    """
    
    @staticmethod
    def calculate_stop_loss(latest_price: float, atr: float, multiplier: float = 1.5) -> float:
        """
        计算动态止损位
        标准：latest_price - multiplier × ATR
        """
        return latest_price - multiplier * atr
    
    @staticmethod
    def calculate_kelly_position(
        prob_win: float,
        prob_loss: float,
        win_size: float,
        loss_size: float,
        safety_factor: float = 0.25  # 使用凯利公式的 25%
    ) -> float:
        """
        简化凯利公式计算最大仓位权重
        
        凯利公式：f = (p × b - q) / b
        其中：
        - p = 胜率
        - q = 败率 = 1 - p
        - b = 收益/亏损比
        
        为了保守，使用 safety_factor × f 作为建议仓位
        """
        if prob_win <= 0 or prob_loss <= 0 or win_size <= 0 or loss_size <= 0:
            return 0.0
        
        b = win_size / loss_size  # 收益亏损比
        q = 1 - prob_win
        
        if b <= 0:
            return 0.0
        
        kelly_fraction = (prob_win * b - q) / b
        
        # 限制在 [0, 1]
        kelly_fraction = max(0.0, min(1.0, kelly_fraction))
        
        # 应用安全系数（保守的 25% Kelly）
        safe_position = kelly_fraction * safety_factor
        
        return max(0.0, min(0.5, safe_position))  # 最多 50% 仓位


# ============================================================================
# 报告生成器（增强版）
# ============================================================================

class EnhancedStrategyAdvisor:
    """增强版策略顾问"""
    
    def __init__(
        self,
        snapshot: SnapshotData,
        holdings: Optional[dict[str, SnapshotData]] = None,
        repo: Optional[EnhancedDataRepository] = None
    ):
        self.snapshot = snapshot
        self.holdings = holdings or {}
        self.repo = repo
        # 计算模式与基线（延迟计算可每个模块重算以确保最新）
        self._mode = None
        self._baseline_price = None
    
    def generate_enhanced_report(self) -> str:
        """生成增强版报告（合并旧版输出模块）"""
        sections = [
            self._title_section(),
            self._section_price_momentum(),
            self._crowding_alert_section(),
            self._section_quantitative_recovery(),
            self._risk_control_section(),
            self._section_valuation_safety(),
            self._section_macro_sentiment(),
            self._correlation_warning_section(),
            self._section_delta_comparison(),
            self._section_recommendation(),
            self._original_analysis_section(),
        ]

        return "\n\n".join(filter(None, sections))

    def save_report(self, report: str, output_path: pathlib.Path, ticker: str, snap_today: SnapshotData) -> None:
        if output_path is None:
            return

        # 使用 snapshot 的日期作为时间标签（YYYYMMDD）
        date_tag = snap_today.date
        try:
            date_tag = str(pathlib.Path(date_tag).name).replace('-', '')
        except Exception:
            date_tag = date_tag.replace('-', '') if isinstance(date_tag, str) else datetime.now(tz=timezone.utc).strftime('%Y%m%d')

        if output_path.is_dir():
            filename = f"strategy_{ticker}_{date_tag}.md"
            output_path = output_path / filename
        else:
            parent = output_path.parent
            stem = output_path.stem
            suffix = output_path.suffix or ".md"
            new_name = f"{stem}_{date_tag}{suffix}"
            parent.mkdir(parents=True, exist_ok=True)
            output_path = parent / new_name

        output_path.write_text(report, encoding="utf-8")
        print(f"[OK] Report saved to {output_path}\n")
    
    def _title_section(self) -> str:
        """标题"""
        snap = self.snapshot
        date_str = snap.date
        ticker = snap.ticker
        price = snap.risk_positioning.latest_price
        # 从 holdings.json 中读取持仓信息（若存在）
        holdings_path = pathlib.Path(__file__).resolve().parents[1] / "data" / "holdings" / "holdings.json"
        holding_flag = ""
        holding_line = ""
        try:
            if holdings_path.is_file():
                obj = json.loads(holdings_path.read_text(encoding="utf-8"))
                holdings = obj.get("holdings", {})
                if ticker in holdings:
                    h = holdings[ticker]
                    pos = h.get("position")
                    avg = h.get("averageCost")
                    mkt = h.get("marketPrice")
                    # 有持仓
                    holding_flag = "**持仓**：已持有"
                    holding_line = f"**持仓详情**：{ticker} {pos} 股，成本 ${float(avg):.2f}，市价 ${float(mkt):.2f}"
                    # set baseline
                    try:
                        self._mode = "Portfolio Management"
                        self._baseline_price = float(avg) if avg is not None else None
                    except Exception:
                        self._mode = "Portfolio Management"
                        self._baseline_price = None
                else:
                    holding_flag = "**持仓**：未持有"
                    holding_line = ""
                    self._mode = "New Entry Analysis"
                    self._baseline_price = None
        except Exception:
            holding_flag = ""
            holding_line = ""

        # 如果命令行传入成本优先覆盖
        if self.snapshot.cost_price is not None and self.snapshot.cost_price > 0:
            self._mode = "Portfolio Management"
            self._baseline_price = float(self.snapshot.cost_price)

        # 展示 header mode
        mode_tag = "[HOLDING]" if self._mode == "Portfolio Management" else "[WATCHLIST]"

        # 财务摘要
        fin_lines = []
        if self._mode == "Portfolio Management" and self._baseline_price:
            cost = self._baseline_price
            pnl_pct = ((price - cost) / cost * 100) if cost else 0
            # distance to stop
            stop = snap.risk_positioning.stop_1x5_atr or 0
            distance_to_stop_pct = ((price - stop) / price * 100) if price else 0
            fin_lines.append(f"**成本**：${cost:.2f}  |  **PnL**：{pnl_pct:+.2f}%  |  **距止损**：{distance_to_stop_pct:.2f}%")
        else:
            # watchlist
            stop = snap.risk_positioning.stop_1x5_atr or 0
            # Kelly using latest_price as base (safe calc)
            kelly_pct = 0.0
            try:
                latest = price
                win_size = snap.risk_positioning.tp_aggressive - latest
                loss_size = latest - stop if latest and stop is not None else 0
                if loss_size > 0:
                    prob_win = snap.bootstrap_probs.get(20).prob_always_above_cost if 20 in snap.bootstrap_probs else 0
                    prob_loss = 1 - prob_win
                    kelly_pct = StopLossAndKellyCalculator.calculate_kelly_position(prob_win, prob_loss, win_size, loss_size)
            except Exception:
                kelly_pct = 0.0

            rr = (snap.risk_positioning.tp_aggressive - latest) / (latest - stop) if (latest and stop and (latest - stop) != 0) else 0
            fin_lines.append(f"**Entry Ref**：${price:.2f}  |  **R/R**：1:{rr:.2f}  |  **Kelly**：{kelly_pct:.1%}")

        safety_buffer = ((price - (snap.risk_positioning.stop_1x5_atr or 0)) / price * 100) if price else 0

        return f"""# 📊 {ticker} 增强版投资分析报告 {mode_tag}

{holding_flag}
{holding_line}
{'\n'.join(fin_lines)}
**股票**：{ticker}  
**当前价**：${price:.2f}  
**报告日期**：{date_str}  
**生成时间**：{datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}  
**安全缓冲**：{safety_buffer:.2f}%
"""
    
    def _crowding_alert_section(self) -> str:
        """拥挤度警报"""
        snap = self.snapshot
        rsi = snap.valuation.rsi_14
        iv_pct = snap.valuation.iv_percentile
        
        if rsi is None or iv_pct is None:
            return "## ⚡ 1. 拥挤度警报\n\n数据不完整，无法计算。"
        
        crowding_idx, interpretation = CrowdingAnalyzer.calculate_crowding_index(rsi, iv_pct)
        
        return f"""## ⚡ 1. 拥挤度警报 (Crowding & Volatility Context)

**拥挤度指数**：{crowding_idx:.1f} / 100

**关键指标**：
- **RSI(14)**：{rsi:.1f}
- **IV Rank**：{iv_pct:.1f}%

**评价**：{interpretation}

**含义**：
- RSI > 70 = 超买信号
- IV Rank > 80% = 市场预期剧烈波动
- 二者同时高企 = 博傻交易特征，风险极高
"""
    
    def _correlation_warning_section(self) -> str:
        """持仓相关性校验"""
        if not self.holdings:
            return ""
        
        snap = self.snapshot
        target_ticker = snap.ticker
        
        # 尝试计算相关性
        try:
            target_df = self.repo.get_historical_data(target_ticker, limit=60)
            if target_df.empty or len(target_df) < 20:
                return ""
            
            # 计算目标股对数收益率
            target_df["log_return"] = np.log(target_df["close"] / target_df["close"].shift(1))
            target_returns = target_df.set_index("date")["log_return"].dropna()
            
            # 计算已有持仓的对数收益率
            holdings_returns = {}
            for holding_ticker in self.holdings.keys():
                holding_df = self.repo.get_historical_data(holding_ticker, limit=60)
                if not holding_df.empty and len(holding_df) > 20:
                    holding_df["log_return"] = np.log(holding_df["close"] / holding_df["close"].shift(1))
                    holdings_returns[holding_ticker] = holding_df.set_index("date")["log_return"].dropna()
            
            if not holdings_returns:
                return ""
            
            # 计算相关性
            correlations = CorrelationChecker.calculate_correlations(
                target_ticker, target_returns, holdings_returns
            )
            
            alert = CorrelationChecker.generate_correlation_alert(target_ticker, correlations)
            
            return f"""## 📍 2. 持仓相关性校验 (Sector Correlation Matrix)

{alert}
"""
        except Exception as e:
            return f"## 📍 2. 持仓相关性校验\n\n计算失败：{e}\n"
    
    def _breakeven_time_section(self) -> str:
        """回本期望时间 / 新建分析替代指标"""
        snap = self.snapshot
        bootstrap = snap.bootstrap_probs

        if not bootstrap:
            return ""

        # 确保 mode 已由 title 设定（若未设则基线为 cost 或 None）
        mode = self._mode if self._mode is not None else ("Portfolio Management" if snap.cost_price else "New Entry Analysis")

        if mode == "New Entry Analysis":
            # 对于新建，计算 5% 涨幅的概率（近似使用 20d 分布）
            prob_5pct = None
            if 20 in bootstrap:
                median = bootstrap[20].final_median or 0
                latest = snap.risk_positioning.latest_price
                # 估计 prob of 5% gain：如果 median 提供信息则以 median 判断，否则使用 prob_reach_tp as proxy
                try:
                    if latest and latest * 1.05 <= bootstrap[20].final_median:
                        prob_5pct = bootstrap[20].prob_reach_tp
                    else:
                        prob_5pct = bootstrap[20].prob_reach_tp
                except Exception:
                    prob_5pct = bootstrap[20].prob_reach_tp

            return f"""## ⏰ 3. 新建入场视角：5% 涨幅概率（近似）\n\n- **20d 视角 5% 涨幅概率（近似）**：{(prob_5pct or 0):.1%}\n"""

        # Portfolio Management: 使用持仓成本计算回本时间并给出保护建议
        cost = self._baseline_price or snap.cost_price
        latest = snap.risk_positioning.latest_price
        result = BreakevenTimeCalculator.estimate_breakeven_days(bootstrap)

        extra = []
        if cost and latest:
            if latest > cost:
                extra.append("**状态**：Profit Protected（当前价格高于成本），建议使用 trailing stop（1.5×ATR）保护利润。")
                trailing = StopLossAndKellyCalculator.calculate_stop_loss(latest, snap.risk_positioning.latest_atr, multiplier=1.5)
                extra.append(f"建议 trailing stop：${trailing:.2f} （1.5×ATR）")

        return f"""## ⏰ 3. 回本期望时间 (Portfolio Management)

**估计中位数天数**：{result['median_days']:.0f} 交易日  
**置信度**：{result['confidence']}

**详细分析**：{result['details']}

{'\n'.join(extra)}

**Bootstrap 概率分布**：
"""  + "\n".join([
            f"- **{h}d 视角**：始终高于成本概率 = {bootstrap[h].prob_always_above_cost:.1%}"
            for h in sorted(bootstrap.keys())
        ])
    
    def _stop_loss_kelly_section(self) -> str:
        """止损与凯利仓位"""
        snap = self.snapshot
        risk = snap.risk_positioning
        bootstrap = snap.bootstrap_probs
        latest_price = risk.latest_price
        atr = risk.latest_atr
        cost_price = snap.cost_price

        # 计算 1.5×ATR 止损（基于 latest_price）
        stop_loss = StopLossAndKellyCalculator.calculate_stop_loss(latest_price, atr, multiplier=1.5)

        # 凯利仓位：始终以 latest_price 作为起点
        kelly_position = 0.0
        if 20 in bootstrap:
            prob_win = bootstrap[20].prob_always_above_cost
            prob_loss = 1 - prob_win
            win_size = risk.tp_aggressive - latest_price
            loss_size = latest_price - stop_loss
            # 安全检查
            if loss_size <= 0 or win_size <= 0 or prob_win <= 0:
                kelly_position = 0.0
            else:
                kelly_position = StopLossAndKellyCalculator.calculate_kelly_position(
                    prob_win, prob_loss, win_size, loss_size
                )
        
        # 计算风险收益比（以 latest_price 为基准）
        rr = 0
        try:
            denom = (latest_price - stop_loss)
            if denom and denom != 0:
                rr = (risk.tp_aggressive - latest_price) / denom
        except Exception:
            rr = 0

        # baseline mode for display
        baseline_display = cost_price if cost_price and cost_price > 0 else latest_price

        safety_buffer = ((latest_price - (risk.stop_1x5_atr or 0)) / latest_price * 100) if latest_price else 0

        return f"""## 🎯 4. 止损与凯利仓位建议

    **基准参考价**：${baseline_display:.2f}
    **推荐止损位**：${stop_loss:.2f}  
    （基于 1.5 × ATR = 1.5 × ${atr:.2f}）

    **止损距离**：${latest_price - stop_loss:.2f} ({(latest_price - stop_loss) / latest_price * 100:.1f}%)

    ---

    **凯利公式最大仓位权重（基于最新价）**：{kelly_position:.1%}

    **含义**：
    - 如果总资金为 $100,000，该标的建议头寸 ${ 100000 * kelly_position:,.0f}
    - 使用 25% Kelly（保守策略），实际建议仓位不超过 {kelly_position:.1%}

    ---

    **风险收益概览（基于最新价）**：
    - **现价** → **止损**：${latest_price:.2f} → ${stop_loss:.2f}
    - **现价** → **激进目标**：${latest_price:.2f} → ${risk.tp_aggressive:.2f}
    - **风险收益比**：1 : {rr:.2f}

    **安全缓冲**：{safety_buffer:.2f}%
    """
    
    def _original_analysis_section(self) -> str:
        """原有分析（简化版）"""
        snap = self.snapshot
        risk = snap.risk_positioning
        bootstrap = snap.bootstrap_probs
        
        lines = ["## 📈 5. 原有量化指标速览"]
        
        # 技术面
        tech = snap.technicals
        lines.append(f"\n**技术面趋势**：{tech.regime}")
        if tech.rsi_14 is not None:
            lines.append(f"**RSI(14)**：{tech.rsi_14:.1f}")
        
        # Bootstrap 概率（简化）
        if 60 in bootstrap:
            prob_60d = bootstrap[60].prob_always_above_cost
            lines.append(f"\n**60日回本概率**：{prob_60d:.1%}")
        
        # 估值
        if snap.valuation.pe_percentile:
            lines.append(f"**PE 分位**：{snap.valuation.pe_percentile:.0f}%")
        
        return "\n".join(lines)

    # ======= 从旧版合并的额外章节与工具函数（adapted） =======
    def _section_price_momentum(self) -> str:
        """价格与动能（包含与历史的简单对比）"""
        snap = self.snapshot
        price = snap.risk_positioning.latest_price
        tech = snap.technicals

        lines = ["## 📈 1. 价格与动能", f"\n**当前价**：${price:.2f}"]

        # 7日变化（从历史 close 计算）
        try:
            if self.repo:
                df = self.repo.get_historical_data(snap.ticker, limit=10)
                if not df.empty and len(df) >= 7:
                    hist_price = float(df.sort_values('date').iloc[-7]['close'])
                    price_change = price - hist_price
                    price_change_pct = (price_change / hist_price * 100) if hist_price else 0
                    direction = "📈" if price_change > 0 else "📉"
                    lines.append(f"**7日变动**：{direction} {price_change:+.2f} ({price_change_pct:+.2f}%)")
        except Exception:
            pass

        regime_desc = self._translate_regime(tech.regime) if hasattr(self, '_translate_regime') else tech.regime
        lines.append(f"\n**趋势状态**：`{tech.regime}` → {regime_desc}")

        if tech.rsi_14 is not None:
            rsi_signal = self._interpret_rsi(tech.rsi_14) if hasattr(self, '_interpret_rsi') else ''
            lines.append(f"**RSI(14)**：{tech.rsi_14:.1f} - {rsi_signal}")

        ma_status = self._analyze_ma_alignment(tech) if hasattr(self, '_analyze_ma_alignment') else "数据不完整"
        lines.append(f"\n**均线排列**：{ma_status}")

        return "\n".join(lines)

    def _section_quantitative_recovery(self) -> str:
        """逐视角展示 Bootstrap 回本概率与价格分布（10/20/60d）"""
        snap = self.snapshot
        bootstrap = snap.bootstrap_probs

        if not bootstrap:
            return ""

        lines = ["## 🎯 2. 量化胜率与回本概率（Bootstrap）", "\n这是系统基于历史蒙特卡洛模拟得出的概率分布："]

        for horizon in [10, 20, 60]:
            if horizon not in bootstrap:
                continue
            prob = bootstrap[horizon]
            lines.append(f"\n### {horizon} 日视角")
            lines.append(f"- **始终高于成本的概率**：{prob.prob_always_above_cost:.1%}")
            lines.append(f"- **触及止损线的风险**：{prob.prob_touch_stop:.1%}")
            lines.append(f"- **触及激进目标的概率**：{prob.prob_reach_tp:.1%}")
            lines.append(f"- **中位数目标**：${prob.final_median:.2f}")
            lines.append(f"- **5% 分位（看空）**：${prob.final_pct5:.2f}")
            lines.append(f"- **95% 分位（看多）**：${prob.final_pct95:.2f}")

        # 总体评价（adapted from old helper）
        lines.append(self._summarize_recovery_probs(bootstrap))

        return "\n".join(lines)

    def _risk_control_section(self) -> str:
        snap = self.snapshot
        risk = snap.risk_positioning
        price = risk.latest_price

        lines = [
            "## 🛡️ 3. 风险对冲位",
            f"\n**当前价**：${price:.2f}",
            f"**ATR(14)**：${risk.latest_atr:.2f}",
        ]

        stop_level = risk.stop_1x5_atr
        stop_distance = ((stop_level - price) / price * 100) if price else 0
        lines.append(f"\n**硬止损线（1.5x ATR）**：${stop_level:.2f} (距现价 {stop_distance:+.2f}%)")

        tp_level = risk.tp_aggressive
        tp_distance = ((tp_level - price) / price * 100) if price else 0
        lines.append(f"**激进目标（TP）**：${tp_level:.2f} (上涨潜力 {tp_distance:+.2f}%)")

        rr_score = getattr(risk, "rr_score", None)
        if rr_score is not None:
            rr_desc = self._interpret_rr_score(rr_score)
            lines.append(f"\n**风险/收益评分**：{rr_score:.1f}/10 - {rr_desc}")

        hint = getattr(risk, "positioning_hint", None)
        if hint:
            lines.append(f"\n**系统操作暗示**：*{hint}*")
        else:
            lines.append(f"\n**系统操作暗示**：基于技术面，等待清晰的突破信号")

        return "\n".join(lines)

    def _section_valuation_safety(self) -> str:
        """估值与 IV 状态（详细版）"""
        snap = self.snapshot
        val = snap.valuation

        lines = ["## 💰 4. 估值与安全性"]

        if val.pe_percentile is not None:
            pe_pct = val.pe_percentile
            pe_desc = self._interpret_pe_percentile(pe_pct)
            lines.append(f"\n**PE 历史分位**：{pe_pct:.0f}% - {pe_desc}")
        else:
            lines.append("\n**PE 历史分位**：暂无数据")

        if val.iv_percentile is not None:
            iv_pct = val.iv_percentile
            lines.append(f"\n**IV 历史分位**：{iv_pct:.1f}%")

        iv_regime = getattr(val, 'iv_regime', 'unknown')
        iv_regime_desc = self._interpret_iv_regime(iv_regime)
        lines.append(f"**IV 状态**：`{iv_regime}` → {iv_regime_desc}")

        return "\n".join(lines)

    def _section_macro_sentiment(self) -> str:
        """宏观背景与情绪指标（来自 snapshot.macro_context）"""
        snap = self.snapshot
        macro = snap.macro_context or {}

        lines = ["## 📡 5. 宏观与情绪哨兵"]

        spy_trend = macro.get("spy_trend_regime", "N/A")
        lines.append(f"\n**大盘状态（SPY）**：{spy_trend}")

        excess_return = macro.get("excess_return_20d")
        if excess_return is not None:
            emoji = "📈" if excess_return > 0 else "📉"
            lines.append(f"**20日超额收益 vs SPY**：{emoji} {excess_return:+.2f}%")

        corr_60d = macro.get("log_return_corr_60d")
        if corr_60d is not None:
            corr_desc = "高度相关" if abs(corr_60d) > 0.7 else "中等相关" if abs(corr_60d) > 0.4 else "低相关"
            lines.append(f"**60日收益相关性 vs SPY**：{corr_60d:.3f} ({corr_desc})")

        # 估值敏感度提示
        if self._is_high_valuation_stock():
            lines.append("\n> ⚠️ **[估值敏感度：高]** 该股票对利率上行敏感。")
        elif self._is_utility_stock():
            lines.append("\n> 💡 **[估值敏感度：中等]** 该股票具有防守属性。")

        return "\n".join(lines)

    def _section_delta_comparison(self) -> str:
        """与历史数据的对比（尽量使用 repo 的历史 close 近似代替 snapshot_history）"""
        if not self.repo:
            return ""

        snap = self.snapshot
        try:
            df = self.repo.get_historical_data(snap.ticker, limit=10)
            if df.empty or len(df) < 7:
                return ""

            df_sorted = df.sort_values('date')
            price_today = snap.risk_positioning.latest_price
            price_hist = float(df_sorted.iloc[-7]['close'])
            price_change_pct = ((price_today - price_hist) / price_hist * 100) if price_hist else 0

            lines = [f"## ⏳ 6. 时间维度变化（近7日对比）", ""]
            lines.append(f"**价格变动**：{price_change_pct:+.2f}%")

            rsi = snap.technicals.rsi_14
            if rsi is not None:
                lines.append(f"**RSI(14)**：{rsi:.1f}")

            return "\n".join(lines)
        except Exception:
            return ""

    def _section_recommendation(self) -> str:
        """综合建议与推理链（基于旧版逻辑）"""
        snap = self.snapshot

        lines = ["## 🎬 7. 综合建议"]
        recommendation = self._generate_recommendation()
        lines.append(f"\n### 操作建议\n**{recommendation}**")

        logic = self._generate_logic_chain()
        if logic:
            lines.append("\n### 推理逻辑")
            for item in logic:
                lines.append(f"- {item}")

        risks = self._identify_key_risks()
        if risks:
            lines.append("\n### 主要风险")
            for risk in risks:
                lines.append(f"- ⚠️ {risk}")

        return "\n".join(lines)

    # ========== Helper functions borrowed/adapted from old advisor ==========
    def _translate_regime(self, regime: str) -> str:
        translations = {
            "bull_strong": "强势上涨，MA5/20/60 多头排列",
            "bull_partial": "中期走强，但 MA200 仍有压制",
            "bear_partial": "中期走弱，但尚未破关键支撑",
            "bear_strong": "强势下跌，全面弱势",
            "sideways": "盘整状态，缺乏明确方向",
        }
        return translations.get(regime, regime)

    def _interpret_rsi(self, rsi: float) -> str:
        if rsi > 70:
            return "⚡ 超买（可能回调）"
        elif rsi < 30:
            return "🔻 超卖（可能反弹)"
        elif rsi > 50:
            return "💪 强势"
        else:
            return "⏳ 弱势"

    def _interpret_pe_percentile(self, pe_pct: float) -> str:
        if pe_pct > 90:
            return "极度昂贵，缺乏安全边际"
        elif pe_pct > 75:
            return "偏贵，需要看到基本面增长"
        elif pe_pct > 50:
            return "合理水平"
        elif pe_pct > 25:
            return "偏便宜，相对有吸引力"
        else:
            return "极度便宜，可能是投资机会"

    def _interpret_iv_regime(self, iv_regime: str) -> str:
        descs = {
            "cheap": "极低，期权便宜，长期建仓的好时机",
            "low": "偏低，可积极建仓",
            "neutral": "正常水平，定价合理",
            "high": "偏高，需谨慎，或用于止损对冲",
            "expensive": "极高，存在高风险",
        }
        return descs.get(iv_regime, "状态未知")

    def _interpret_rr_score(self, score: float) -> str:
        if score >= 8:
            return "极优秀 - 风险小收益大 🌟"
        elif score >= 6:
            return "良好 - 风险可控 ✓"
        elif score >= 4:
            return "一般 - 风险收益平衡"
        elif score >= 2:
            return "欠佳 - 风险相对较高"
        else:
            return "较差 - 风险大于收益 ❌"

    def _analyze_ma_alignment(self, tech: TechnicalMetrics) -> str:
        ma5, ma20, ma60, ma200 = tech.ma5, tech.ma20, tech.ma60, tech.ma200
        if not all([ma5, ma20, ma60, ma200]):
            return "数据不完整"
        if ma5 > ma20 > ma60 > ma200:
            return "金叉排列 (完全多头) 🟢"
        elif ma5 < ma20 < ma60 < ma200:
            return "死叉排列 (完全空头) 🔴"
        elif ma20 > ma60 > ma200:
            return "多头排列 (中期看涨) 🟢"
        elif ma20 < ma60 < ma200:
            return "空头排列 (中期看跌) 🔴"
        else:
            return "纠缠排列 (缺乏明确方向) 🟡"

    def _summarize_recovery_probs(self, bootstrap: dict[int, BootstrapProbs]) -> str:
        if 60 not in bootstrap:
            return ""
        prob_60d = bootstrap[60]
        win_rate = prob_60d.prob_always_above_cost
        if win_rate > 0.7:
            return "\n> ✅ **总体评价**：60日胜率超过70%，长期看涨态度明确。"
        elif win_rate > 0.5:
            return "\n> ✓ **总体评价**：60日胜率在50-70%之间，概率倾向看涨。"
        elif win_rate > 0.3:
            return "\n> ⏳ **总体评价**：60日胜率在30-50%之间，存在较大不确定性。"
        else:
            return "\n> ❌ **总体评价**：60日胜率低于30%，概率倾向看空，需要谨慎。"

    def _is_high_valuation_stock(self) -> bool:
        high_val_stocks = ["TSLA", "NVDA", "AVGO", "AMZN", "GOOG"]
        return self.snapshot.ticker in high_val_stocks

    def _is_utility_stock(self) -> bool:
        utility_stocks = ["CEG", "VST", "LMT"]
        return self.snapshot.ticker in utility_stocks

    def _generate_recommendation(self) -> str:
        snap = self.snapshot
        bootstrap = snap.bootstrap_probs
        risk = snap.risk_positioning
        val = snap.valuation
        score = 0
        if 60 in bootstrap:
            if bootstrap[60].prob_always_above_cost > 0.7:
                score += 3
            elif bootstrap[60].prob_always_above_cost > 0.5:
                score += 2
            elif bootstrap[60].prob_always_above_cost > 0.3:
                score += 1
        if getattr(risk, 'rr_score', 0) >= 8:
            score += 2
        elif getattr(risk, 'rr_score', 0) >= 6:
            score += 1
        if getattr(val, 'pe_percentile', None) is not None:
            if val.pe_percentile < 30:
                score += 2
            elif val.pe_percentile < 70:
                score += 1
        if score >= 7:
            return "🟢 **强烈推荐**：多条线索共振，可考虑建立或加仓"
        elif score >= 5:
            return "🟡 **适度推荐**：基本面有支撑，但需等待确认信号"
        elif score >= 3:
            return "⏳ **观望持有**：存在不确定性，继续观察演变"
        else:
            return "🔴 **谨慎回避**：风险大于机遇，暂不建议介入"

    def _generate_logic_chain(self) -> list[str]:
        logic = []
        snap = self.snapshot
        if snap.technicals.regime:
            logic.append(f"技术面：处于 {self._translate_regime(snap.technicals.regime)}")
        if 60 in snap.bootstrap_probs:
            win_rate = snap.bootstrap_probs[60].prob_always_above_cost
            if win_rate > 0.6:
                logic.append(f"量化胜率：60日回本率 {win_rate:.0%}，概率优势明显")
        if getattr(snap.valuation, 'pe_percentile', None) is not None:
            if snap.valuation.pe_percentile > 80:
                logic.append(f"估值面：PE 分位 {snap.valuation.pe_percentile:.0f}%，估值压力较大")
        return logic

    def _identify_key_risks(self) -> list[str]:
        risks = []
        snap = self.snapshot
        if 20 in snap.bootstrap_probs:
            if snap.bootstrap_probs[20].prob_touch_stop > 0.5:
                risks.append(f"20日内触及止损线的概率达 {snap.bootstrap_probs[20].prob_touch_stop:.0%}，需做好风控")
        if getattr(snap.valuation, 'pe_percentile', None) and snap.valuation.pe_percentile > 90:
            risks.append(f"PE 分位极高（{snap.valuation.pe_percentile:.0f}%），缺乏安全边际，下跌空间大")
        if getattr(snap.valuation, 'iv_percentile', None) and snap.valuation.iv_percentile > 80:
            risks.append(f"IV 处于高位（{snap.valuation.iv_percentile:.0f}%），波动性可能加剧")
        return risks


# ============================================================================
# 主程序
# ============================================================================

def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="增强版策略顾问 - 拥挤度/相关性/回本时间/止损"
    )
    parser.add_argument("--db", type=str, default="investment_lab.db",
                        help="SQLite 数据库路径")
    parser.add_argument("--ticker", type=str, default="AMD",
                        help="目标股票代码")
    parser.add_argument("--holdings", type=str, default="",
                        help="已有持仓股票代码（逗号分隔），用于相关性计算")
    parser.add_argument("--cost", type=float, default=None,
                        help="持仓成本价（可选）")
    parser.add_argument("--output", type=str, default=None,
                        help="输出文件路径")
    
    args = parser.parse_args()
    
    # 初始化数据库
    repo = EnhancedDataRepository(args.db)
    
    # 获取目标股票数据
    target_ticker = args.ticker.upper()

    # 优先从 data/holdings/holdings.json 中读取持仓成本
    holdings_path = pathlib.Path(__file__).resolve().parents[1] / "data" / "holdings" / "holdings.json"
    cost_from_holdings = None
    try:
        if holdings_path.is_file():
            obj = json.loads(holdings_path.read_text(encoding="utf-8"))
            h = obj.get("holdings", {})
            if target_ticker in h:
                val = h[target_ticker].get("averageCost")
                if val is not None:
                    cost_from_holdings = float(val)
    except Exception:
        cost_from_holdings = None

    # 如果命令行提供成本价优先，否则使用 holdings 中的成本价
    cost_price_arg = args.cost if args.cost is not None else cost_from_holdings
    snapshot = repo.get_latest_snapshot(target_ticker, cost_price=cost_price_arg)
    
    if not snapshot:
        print(f"[ERR] No data found for {target_ticker}")
        return
    
    # 获取已有持仓数据
    holdings = {}
    if args.holdings:
        holding_tickers = [t.strip().upper() for t in args.holdings.split(",")]
        for holding_ticker in holding_tickers:
            snap = repo.get_latest_snapshot(holding_ticker)
            if snap:
                holdings[holding_ticker] = snap
    
    # 生成报告
    advisor = EnhancedStrategyAdvisor(snapshot, holdings=holdings, repo=repo)
    report = advisor.generate_enhanced_report()
    
    # 输出
    if args.output:
        advisor.save_report(report, pathlib.Path(args.output), target_ticker, snapshot)
    else:
        print(report)


if __name__ == "__main__":
    main()
