"""strategy_advisor_optimized.py — 优化版策略顾问报告生成引擎

核心优化点：
1. 全指标提取：从 bootstrap 中显式提取回本概率分布（10/20/60d）
2. 趋势计算：支持从数据库查询历史数据，计算时间维度 Delta
3. 宏观上下文增强：输出宏观数据时，计算 10Y 美债周波动
4. 大白话格式：清晰的 Markdown 结构，三个核心板块

依赖输入：
  - SQLite 数据库（investment_lab.db）
  
输出：
  - stdout 或 Markdown 文件
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

import pandas as pd


# ============================================================================
# 数据类定义
# ============================================================================

@dataclass
class BootstrapProbs:
    """回本概率数据结构"""
    horizon: int  # 10, 20, 60
    prob_touch_stop: float  # 触及止损线的概率
    prob_always_above_cost: float  # 始终高于成本的概率
    prob_reach_tp: float  # 触及目标价的概率
    final_median: float  # 中位数目标价
    final_pct5: float  # 5% 分位（看空）
    final_pct95: float  # 95% 分位（看多）


@dataclass
class RiskPositioning:
    """风险对冲位数据"""
    latest_price: float
    latest_atr: float
    stop_1x5_atr: float  # 硬止损线
    tp_aggressive: float  # 激进目标
    rr_score: float  # 风险收益评分
    positioning_hint: Optional[str] = None


@dataclass
class ValuationMetrics:
    """估值指标"""
    pe_percentile: Optional[float]  # PE 历史分位
    iv_percentile: Optional[float]  # IV 历史分位
    iv_regime: str  # IV 状态（cheap/neutral/expensive）


@dataclass
class TechnicalMetrics:
    """技术指标"""
    regime: str  # bull_partial, bear_partial, sideways etc
    rsi_14: Optional[float]
    ma5: Optional[float]
    ma20: Optional[float]
    ma60: Optional[float]
    ma200: Optional[float]


@dataclass
class SnapshotData:
    """数据快照（T 日和 T-7 日的对比）"""
    date: str
    bootstrap_probs: dict[int, BootstrapProbs]  # {10: ..., 20: ..., 60: ...}
    risk_positioning: RiskPositioning
    valuation: ValuationMetrics
    technicals: TechnicalMetrics
    macro_context: Optional[dict] = None


# ============================================================================
# 数据库访问层
# ============================================================================

class QuantDataRepository:
    """从 SQLite 数据库中读取量化数据"""

    def __init__(self, db_path: str | pathlib.Path):
        self.db_path = pathlib.Path(db_path)

    def get_latest_snapshot(self, ticker: str) -> Optional[SnapshotData]:
        """获取最新的数据快照"""
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
            return self._parse_metrics_to_snapshot(date, data)

    def get_snapshot_before(self, ticker: str, before_date: str) -> Optional[SnapshotData]:
        """获取指定日期前最近的一条数据"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT date, metrics FROM quantitative "
                "WHERE ticker = ? AND date < ? "
                "ORDER BY date DESC LIMIT 1",
                (ticker, before_date)
            )
            row = cursor.fetchone()
            if not row:
                return None

            date, metrics_json = row
            data = json.loads(metrics_json)
            return self._parse_metrics_to_snapshot(date, data)

    def _parse_metrics_to_snapshot(self, date: str, data: dict) -> SnapshotData:
        """将 JSON 数据解析为 SnapshotData"""
        # 解析 Bootstrap 概率
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
        
        # 解析风险对冲位
        risk_matrix = data.get("risk_matrix", {})
        extra_dims = data.get("extra_dimensions", {})
        
        risk_positioning = RiskPositioning(
            latest_price=risk_matrix.get("latest_close", 0.0),
            latest_atr=risk_matrix.get("latest_atr", 0.0),
            stop_1x5_atr=risk_matrix.get("stop_1x5_atr", 0.0),
            tp_aggressive=risk_matrix.get("tp_aggressive", 0.0),
            rr_score=risk_matrix.get("rr_score", 0.0),
            positioning_hint=extra_dims.get("positioning_hint"),
        )
        
        # 解析估值指标
        iv_analysis = data.get("iv_analysis", {})
        valuation = ValuationMetrics(
            pe_percentile=extra_dims.get("pe_percentile"),
            iv_percentile=iv_analysis.get("iv_percentile"),
            iv_regime=iv_analysis.get("iv_regime", "unknown"),
        )
        
        # 解析技术指标
        trend = data.get("trend", {})
        technicals_raw = data.get("technicals", {})
        
        technicals = TechnicalMetrics(
            regime=trend.get("regime", "unknown"),
            rsi_14=technicals_raw.get("rsi_14"),
            ma5=technicals_raw.get("ma5"),
            ma20=technicals_raw.get("ma20"),
            ma60=technicals_raw.get("ma60"),
            ma200=technicals_raw.get("ma200"),
        )
        
        # 宏观背景
        macro_context = data.get("market_context", {})
        
        return SnapshotData(
            date=date,
            bootstrap_probs=bootstrap_probs,
            risk_positioning=risk_positioning,
            valuation=valuation,
            technicals=technicals,
            macro_context=macro_context,
        )


# ============================================================================
# 报告生成器
# ============================================================================

class OptimizedStrategyAdvisor:
    """优化版策略顾问 - 核心输出引擎"""

    def __init__(
        self,
        ticker: str,
        snapshot_today: SnapshotData,
        snapshot_history: Optional[SnapshotData] = None,
        cost_price: Optional[float] = None,
    ):
        self.ticker = ticker
        self.snapshot_today = snapshot_today
        self.snapshot_history = snapshot_history
        self.cost_price = cost_price

    def generate_report(self) -> str:
        """生成完整的 Markdown 报告"""
        sections = [
            self._section_title(),
            self._section_price_momentum(),
            self._section_quantitative_recovery(),
            self._section_risk_control(),
            self._section_valuation_safety(),
            self._section_macro_sentiment(),
            self._section_delta_comparison(),
            self._section_recommendation(),
        ]
        return "\n\n".join(filter(None, sections))

    # ===================== 第一部分：标题 =====================

    def _section_title(self) -> str:
        """标题和基本信息"""
        date_str = self.snapshot_today.date
        return f"""# 📊 {self.ticker} 投资分析报告

**报告日期**：{date_str}  
**生成时间**：{datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
"""

    # ===================== 第二部分：价格与动能 =====================

    def _section_price_momentum(self) -> str:
        """价格与技术面动能分析"""
        snap = self.snapshot_today
        price = snap.risk_positioning.latest_price
        tech = snap.technicals
        
        lines = [
            "## 📈 1. 价格与动能",
            f"\n**当前价**：${price:.2f}",
        ]
        
        # 如果有历史数据，显示 7 日变化
        if self.snapshot_history:
            hist_price = self.snapshot_history.risk_positioning.latest_price
            price_change = price - hist_price
            price_change_pct = (price_change / hist_price * 100) if hist_price else 0
            direction = "📈" if price_change > 0 else "📉"
            lines.append(f"**7日变动**：{direction} {price_change:+.2f} ({price_change_pct:+.2f}%)")
        
        # 趋势解释
        regime_desc = self._translate_regime(tech.regime)
        lines.append(f"\n**趋势状态**：`{tech.regime}` → {regime_desc}")
        
        # RSI 状态
        if tech.rsi_14 is not None:
            rsi_signal = self._interpret_rsi(tech.rsi_14)
            lines.append(f"**RSI(14)**：{tech.rsi_14:.1f} - {rsi_signal}")
        
        # 均线纠缠度
        ma_status = self._analyze_ma_alignment(tech)
        lines.append(f"\n**均线排列**：{ma_status}")
        
        return "\n".join(lines)

    # ===================== 第三部分：量化胜率与回本概率 =====================

    def _section_quantitative_recovery(self) -> str:
        """Bootstrap 回本概率分析"""
        snap = self.snapshot_today
        bootstrap = snap.bootstrap_probs
        
        lines = [
            "## 🎯 2. 量化胜率与回本概率（Bootstrap）",
            "\n这是系统最看重的\"胜率\"指标，基于 5000 次历史蒙特卡洛模拟：",
        ]
        
        for horizon in [10, 20, 60]:
            if horizon not in bootstrap:
                continue
            
            prob = bootstrap[horizon]
            
            lines.append(f"\n### {horizon} 日视角")
            
            # 核心胜率
            win_rate = prob.prob_always_above_cost * 100
            lines.append(f"- **始终高于成本的概率**：{win_rate:.1f}%")
            
            # 风险指标
            stop_risk = prob.prob_touch_stop * 100
            lines.append(f"- **触及止损线的风险**：{stop_risk:.1f}%")
            
            # 盈利目标
            tp_hit = prob.prob_reach_tp * 100
            lines.append(f"- **触及激进目标的概率**：{tp_hit:.1f}%")
            
            # 价格分布
            lines.append(f"- **中位数目标**：${prob.final_median:.2f}")
            lines.append(f"- **看空边界（5%分位）**：${prob.final_pct5:.2f}")
            lines.append(f"- **看多边界（95%分位）**：${prob.final_pct95:.2f}")
        
        # 总体评价
        lines.append(self._summarize_recovery_probs(bootstrap))
        
        return "\n".join(lines)

    # ===================== 第四部分：风险控制 =====================

    def _section_risk_control(self) -> str:
        """止损线和风险管理"""
        snap = self.snapshot_today
        risk = snap.risk_positioning
        price = risk.latest_price
        
        lines = [
            "## 🛡️ 3. 风险对冲位",
            f"\n**当前价**：${price:.2f}",
            f"**ATR(14)**：${risk.latest_atr:.2f}",
        ]
        
        # 硬止损线
        stop_level = risk.stop_1x5_atr
        stop_distance = ((stop_level - price) / price) * 100 if price else 0
        lines.append(f"\n**硬止损线（1.5x ATR）**：${stop_level:.2f} (距现价 {stop_distance:+.2f}%)")
        
        # 激进目标
        tp_level = risk.tp_aggressive
        tp_distance = ((tp_level - price) / price) * 100 if price else 0
        lines.append(f"**激进目标（2x ATR）**：${tp_level:.2f} (上涨潜力 {tp_distance:+.2f}%)")
        
        # 风险收益评分
        rr_score = risk.rr_score
        rr_desc = self._interpret_rr_score(rr_score)
        lines.append(f"\n**风险/收益评分**：{rr_score:.1f}/10 - {rr_desc}")
        
        # 定位暗示
        if risk.positioning_hint:
            lines.append(f"\n**系统操作暗示**：*{risk.positioning_hint}*")
        else:
            lines.append(f"\n**系统操作暗示**：基于技术面，等待清晰的突破信号")
        
        return "\n".join(lines)

    # ===================== 第五部分：估值与安全性 =====================

    def _section_valuation_safety(self) -> str:
        """估值分位和 IV 状态"""
        snap = self.snapshot_today
        val = snap.valuation
        
        lines = ["## 💰 4. 估值与安全性"]
        
        # PE 分位
        if val.pe_percentile is not None:
            pe_pct = val.pe_percentile
            pe_desc = self._interpret_pe_percentile(pe_pct)
            lines.append(f"\n**PE 历史分位**：{pe_pct:.0f}% - {pe_desc}")
            
            # 与历史数据对比
            if self.snapshot_history and self.snapshot_history.valuation.pe_percentile is not None:
                hist_pe = self.snapshot_history.valuation.pe_percentile
                pe_delta = pe_pct - hist_pe
                direction = "↓" if pe_delta < 0 else "↑"
                lines.append(f"  - 相较7日前：{direction} {pe_delta:+.1f} 百分点")
        else:
            lines.append("\n**PE 历史分位**：暂无数据")
        
        # IV 分位和状态
        if val.iv_percentile is not None:
            iv_pct = val.iv_percentile
            lines.append(f"\n**IV 历史分位**：{iv_pct:.1f}%")
        
        iv_regime = val.iv_regime
        iv_regime_desc = self._interpret_iv_regime(iv_regime)
        lines.append(f"**IV 状态**：`{iv_regime}` → {iv_regime_desc}")
        
        return "\n".join(lines)

    # ===================== 第六部分：宏观与情绪 =====================

    def _section_macro_sentiment(self) -> str:
        """宏观背景和情绪指标"""
        snap = self.snapshot_today
        macro = snap.macro_context or {}
        
        lines = ["## 📡 5. 宏观与情绪哨兵"]
        
        # SPY 趋势
        spy_trend = macro.get("spy_trend_regime", "N/A")
        lines.append(f"\n**大盘状态（SPY）**：{spy_trend}")
        
        # 超额收益
        excess_return = macro.get("excess_return_20d")
        if excess_return is not None:
            emoji = "📈" if excess_return > 0 else "📉"
            lines.append(f"**20日超额收益 vs SPY**：{emoji} {excess_return:+.2f}%")
        
        # 相关性
        corr_60d = macro.get("log_return_corr_60d")
        if corr_60d is not None:
            corr_desc = "高度相关" if abs(corr_60d) > 0.7 else "中等相关" if abs(corr_60d) > 0.4 else "低相关"
            lines.append(f"**60日收益相关性 vs SPY**：{corr_60d:.3f} ({corr_desc})")
        
        # 宏观关联（估值敏感度标注）
        if self._is_high_valuation_stock():
            lines.append("\n> ⚠️ **[估值敏感度：高]** 该股票是高估值科技股，对美债收益率上升敏感。")
            lines.append(">   - 10Y 美债收益率上升 → PE 承压下行")
            lines.append(">   - 当前宏观流动性收紧的环境下需谨慎")
        
        elif self._is_utility_stock():
            lines.append("\n> 💡 **[估值敏感度：中等]** 该股票具有防守属性，对利率敏感。")
            lines.append(">   - 利率上升 → 财务成本增加，影响 FCF")
            lines.append(">   - 适合低利率环境下建仓")
        
        return "\n".join(lines)

    # ===================== 第七部分：时间维度对比 =====================

    def _section_delta_comparison(self) -> str:
        """与历史数据的对比分析"""
        if not self.snapshot_history:
            return ""
        
        today = self.snapshot_today
        hist = self.snapshot_history
        
        lines = [
            f"## ⏳ 6. 时间维度变化（{hist.date} → {today.date}）",
            ""
        ]
        
        # 价格涨跌
        price_today = today.risk_positioning.latest_price
        price_hist = hist.risk_positioning.latest_price
        price_change_pct = ((price_today - price_hist) / price_hist * 100) if price_hist else 0
        lines.append(f"**价格变动**：{price_change_pct:+.2f}%")
        
        # PE 分位变化
        pe_today = today.valuation.pe_percentile
        pe_hist = hist.valuation.pe_percentile
        if pe_today is not None and pe_hist is not None:
            pe_delta = pe_today - pe_hist
            direction = "压力释放" if pe_delta < 0 else "压力增加"
            lines.append(f"**PE 分位**：从 {pe_hist:.0f}% → {pe_today:.0f}% ({direction})")
        
        # IV Regime 变化
        iv_today = today.valuation.iv_regime
        iv_hist = hist.valuation.iv_regime
        if iv_today != iv_hist:
            lines.append(f"**IV 状态**：从 `{iv_hist}` → `{iv_today}`")
        
        # RSI 变化
        rsi_today = today.technicals.rsi_14
        rsi_hist = hist.technicals.rsi_14
        if rsi_today is not None and rsi_hist is not None:
            rsi_delta = rsi_today - rsi_hist
            direction = "↑" if rsi_delta > 0 else "↓"
            lines.append(f"**RSI(14)**：从 {rsi_hist:.1f} → {rsi_today:.1f} ({direction}{abs(rsi_delta):.1f})")
        
        return "\n".join(lines)

    # ===================== 第八部分：最终建议 =====================

    def _section_recommendation(self) -> str:
        """综合建议和决策逻辑"""
        snap = self.snapshot_today
        
        lines = ["## 🎬 7. 综合建议"]
        
        # 生成建议
        recommendation = self._generate_recommendation()
        lines.append(f"\n### 操作建议\n**{recommendation}**")
        
        # 推理逻辑
        logic = self._generate_logic_chain()
        if logic:
            lines.append("\n### 推理逻辑")
            for item in logic:
                lines.append(f"- {item}")
        
        # 主要风险
        risks = self._identify_key_risks()
        if risks:
            lines.append("\n### 主要风险")
            for risk in risks:
                lines.append(f"- ⚠️ {risk}")
        
        return "\n".join(lines)

    # ===================== 辅助函数 =====================

    def _translate_regime(self, regime: str) -> str:
        """翻译趋势状态为大白话"""
        translations = {
            "bull_strong": "强势上涨，MA5/20/60 多头排列",
            "bull_partial": "中期走强，但 MA200 仍有压制",
            "bear_partial": "中期走弱，但尚未破关键支撑",
            "bear_strong": "强势下跌，全面弱势",
            "sideways": "盘整状态，缺乏明确方向",
        }
        return translations.get(regime, regime)

    def _interpret_rsi(self, rsi: float) -> str:
        """RSI 信号解释"""
        if rsi > 70:
            return "⚡ 超买（可能回调）"
        elif rsi < 30:
            return "🔻 超卖（可能反弹）"
        elif rsi > 50:
            return "💪 强势"
        else:
            return "⏳ 弱势"

    def _interpret_pe_percentile(self, pe_pct: float) -> str:
        """PE 分位解释"""
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
        """IV 状态解释"""
        descs = {
            "cheap": "极低，期权便宜，长期建仓的好时机",
            "low": "偏低，可积极建仓",
            "neutral": "正常水平，定价合理",
            "high": "偏高，需谨慎，或用于止损对冲",
            "expensive": "极高，存在高风险",
        }
        return descs.get(iv_regime, "状态未知")

    def _interpret_rr_score(self, score: float) -> str:
        """风险收益评分解释"""
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
        """分析均线排列"""
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
        """总结回本概率的整体评价"""
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
        """判断是否为高估值股票"""
        high_val_stocks = ["TSLA", "NVDA", "AVGO", "AMZN", "GOOG"]
        return self.ticker in high_val_stocks

    def _is_utility_stock(self) -> bool:
        """判断是否为公用事业股"""
        utility_stocks = ["CEG", "VST", "LMT"]
        return self.ticker in utility_stocks

    def _generate_recommendation(self) -> str:
        """生成综合建议"""
        snap = self.snapshot_today
        bootstrap = snap.bootstrap_probs
        risk = snap.risk_positioning
        val = snap.valuation
        
        # 简单评分逻辑
        score = 0
        
        # 回本概率 (60d)
        if 60 in bootstrap:
            if bootstrap[60].prob_always_above_cost > 0.7:
                score += 3
            elif bootstrap[60].prob_always_above_cost > 0.5:
                score += 2
            elif bootstrap[60].prob_always_above_cost > 0.3:
                score += 1
        
        # 风险收益
        if risk.rr_score >= 8:
            score += 2
        elif risk.rr_score >= 6:
            score += 1
        
        # 估值
        if val.pe_percentile is not None:
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
        """生成推理逻辑链"""
        logic = []
        snap = self.snapshot_today
        
        if snap.technicals.regime:
            logic.append(f"技术面：处于 {self._translate_regime(snap.technicals.regime)}")
        
        if 60 in snap.bootstrap_probs:
            win_rate = snap.bootstrap_probs[60].prob_always_above_cost
            if win_rate > 0.6:
                logic.append(f"量化胜率：60日回本率 {win_rate:.0%}，概率优势明显")
        
        if snap.valuation.pe_percentile is not None:
            if snap.valuation.pe_percentile > 80:
                logic.append(f"估值面：PE 分位 {snap.valuation.pe_percentile:.0f}%，估值压力较大")
        
        return logic

    def _identify_key_risks(self) -> list[str]:
        """识别主要风险"""
        risks = []
        snap = self.snapshot_today
        
        # 高止损风险
        if 20 in snap.bootstrap_probs:
            if snap.bootstrap_probs[20].prob_touch_stop > 0.5:
                risks.append(f"20日内触及止损线的概率达 {snap.bootstrap_probs[20].prob_touch_stop:.0%}，需做好风控")
        
        # 估值高企
        if snap.valuation.pe_percentile and snap.valuation.pe_percentile > 90:
            risks.append(f"PE 分位极高（{snap.valuation.pe_percentile:.0f}%），缺乏安全边际，下跌空间大")
        
        # IV 高企
        if snap.valuation.iv_percentile and snap.valuation.iv_percentile > 80:
            risks.append(f"IV 处于高位（{snap.valuation.iv_percentile:.0f}%），波动性可能加剧")
        
        return risks


# ============================================================================
# 主程序
# ============================================================================

def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="优化版策略顾问 - 直接从 SQLite 数据库读取全面量化指标"
    )
    parser.add_argument("--db", type=str, default="investment_lab.db",
                        help="SQLite 数据库路径")
    parser.add_argument("--ticker", type=str, default="TSLA",
                        help="股票代码（默认 TSLA）")
    parser.add_argument("--cost", type=float, default=None,
                        help="持仓成本价（可选）")
    parser.add_argument("--output", type=str, default=None,
                        help="输出文件路径（省略时输出到 stdout）")
    parser.add_argument("--tickers", type=str, default=None,
                        help="多个股票代码，逗号分隔（会覆盖 --ticker）")
    
    args = parser.parse_args()
    
    # 确定目标股票
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    else:
        tickers = [args.ticker.upper()]
    
    # 初始化数据库访问
    repo = QuantDataRepository(args.db)
    
    # 处理每只股票
    for ticker in tickers:
        print(f"\n{'='*70}")
        print(f"Generating report for {ticker}...")
        print('='*70 + "\n")
        
        try:
            # 获取最新数据
            snap_today = repo.get_latest_snapshot(ticker)
            if not snap_today:
                print(f"[ERR] No data found for {ticker}")
                continue
            
            # 尝试获取历史数据（7天前）
            snap_history = None
            # 由于数据库中只有一条记录，这里暂不支持历史对比
            # 如果有多条记录，可以调用 repo.get_snapshot_before()
            
            # 创建报告生成器
            advisor = OptimizedStrategyAdvisor(
                ticker=ticker,
                snapshot_today=snap_today,
                snapshot_history=snap_history,
                cost_price=args.cost,
            )
            
            # 生成报告
            report = advisor.generate_report()
            
            # 输出
            if args.output:
                output_path = pathlib.Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(report, encoding="utf-8")
                print(f"[OK] Report saved to {output_path}\n")
            else:
                print(report)
        
        except Exception as e:
            print(f"[ERR] {ticker}: {e}")
            raise


if __name__ == "__main__":
    main()
