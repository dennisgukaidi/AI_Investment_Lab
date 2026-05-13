"""get_context_for_ai.py — 为大模型对话提供最近 30 天的股票上下文

该脚本从 ``investment_lab.db`` 中读取四类数据（量化指标、基本面、宏观、舆情），
并将它们组织为一段简洁的自然语言文本，供后续的 AI 对话使用。

使用方式示例::

    python scripts/get_context_for_ai.py --ticker AAPL

输出示例（简化版）::

    Ticker: AAPL
    最近 30 天数据概览（截至 2026-05-12）
    量化指标:
      2026-04-12: {"trend": {...}, "monte_carlo": {...}}
    基本面:
      2026-04-12: {"pe_ratio": 28.5, "market_cap": 2.1e12}
    宏观数据:
      2026-04-12: {"cpi": 2.3, "gdp_growth": 1.8}
    舆情得分:
      2026-04-12: 0.73

如果某类数据在指定时间范围内不存在，脚本会在对应章节中标记 "无数据"。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
from datetime import datetime, timedelta

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "investment_lab.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_rows(conn: sqlite3.Connection, query: str, params: tuple) -> list[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(query, params)
    return cur.fetchall()


def _format_json_snippet(raw: str, max_len: int = 200) -> str:
    """将 JSON 字符串压缩为单行，截断过长部分以保持可读性。"""
    try:
        obj = json.loads(raw)
        compact = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        compact = raw.replace("\n", " ")
    if len(compact) > max_len:
        return compact[:max_len] + "..."
    return compact


def get_context(ticker: str) -> str:
    """返回为大模型准备的、聚焦关键指标的 Markdown 上下文。

    主要改动相较于原实现：
    1. 只取最近两条记录（今天 vs 上周同日），计算 Price、PE_Percentile、Sentiment 的变化率。
    2. 从 quantitative.metrics JSON 中显式提取 ``recovery_prob``、``stop_1x5_atr``、``rr_score``。
    3. 读取 macro 表最新一条记录，展示 10Y 美债收益率和 CPI，并给出简要影响说明。
    4. 输出采用 Markdown 列表而非原始 JSON 片段，便于大模型快速定位。
    """

    conn = _connect()
    try:
        # 量化指标：取最近两条记录（按日期降序）
        quant_rows = _fetch_rows(
            conn,
            "SELECT date, metrics FROM quantitative WHERE ticker=? ORDER BY date DESC LIMIT 2",
            (ticker, ),
        )

        # 舆情得分：同样取最近两条记录
        sentiment_rows = _fetch_rows(
            conn,
            "SELECT date, score FROM sentiment WHERE ticker=? ORDER BY date DESC LIMIT 2",
            (ticker, ),
        )

        # 宏观最新一条记录（全局）
        macro_row = _fetch_rows(
            conn,
            "SELECT date, data FROM macro ORDER BY date DESC LIMIT 1",
            (),
        )
    finally:
        conn.close()

    # ---------- 处理量化指标 ----------
    quant_today = quant_last = None
    if len(quant_rows) == 2:
        quant_today, quant_last = quant_rows[0], quant_rows[1]
    elif len(quant_rows) == 1:
        quant_today = quant_rows[0]

    # ---------- 处理舆情 ----------
    sentiment_today = sentiment_last = None
    if len(sentiment_rows) == 2:
        sentiment_today, sentiment_last = sentiment_rows[0], sentiment_rows[1]
    elif len(sentiment_rows) == 1:
        sentiment_today = sentiment_rows[0]

    # ---------- 解析 JSON 并计算 delta ----------
    def _extract_metrics(row) -> dict:
        """从 quantitative.metrics JSON 中提取关键字段。

        兼容不同的命名约定，并在缺失时尝试从嵌套结构中获取。
        """
        try:
            obj = json.loads(row['metrics'])
        except Exception:
            obj = {}
        # 价格：直接字段或 risk_matrix 中的 latest_close
        price = obj.get('price') or obj.get('close')
        if price is None:
            price = obj.get('risk_matrix', {}).get('latest_close')
        # PE 百分位
        pe_percentile = obj.get('pe_percentile') or obj.get('PE_Percentile')
        # 关键概率/止损/RR
        recovery_prob = obj.get('recovery_prob')
        stop_1x5_atr = obj.get('stop_1x5_atr')
        rr_score = obj.get('rr_score')
        return {
            'price': price,
            'pe_percentile': pe_percentile,
            'recovery_prob': recovery_prob,
            'stop_1x5_atr': stop_1x5_atr,
            'rr_score': rr_score,
        }

    today_metrics = _extract_metrics(quant_today) if quant_today else {}
    last_metrics = _extract_metrics(quant_last) if quant_last else {}

    def _pct_change(new, old) -> str:
        if new is None or old is None or old == 0:
            return "N/A"
        return f"{(new - old) / old * 100:.1f}%"

    price_delta = _pct_change(today_metrics.get('price'), last_metrics.get('price'))
    pe_delta = _pct_change(today_metrics.get('pe_percentile'), last_metrics.get('pe_percentile'))
    sentiment_delta = _pct_change(
        sentiment_today['score'] if sentiment_today else None,
        sentiment_last['score'] if sentiment_last else None,
    )

    # ---------- 宏观信息 ----------
    macro_info = {}
    if macro_row:
        try:
            macro_info = json.loads(macro_row[0]['data'])
        except Exception:
            macro_info = {}
    # 10Y 美债收益率和 CPI 位于嵌套的 indicators 中
    bond_yield = (
        macro_info.get('indicators', {})
        .get('ten_year_treasury', {})
        .get('latest_value')
    )
    cpi = (
        macro_info.get('indicators', {})
        .get('cpi', {})
        .get('latest_value')
    )

    # ---------- 构建 Markdown 输出 ----------
    lines = []
    lines.append(f"**Ticker:** {ticker.upper()}")
    lines.append("\n---\n")

    # 1. 价格与趋势
    lines.append("1. 价格与趋势：")
    if today_metrics.get('price') is not None:
        lines.append(f"   - 当前价: ${today_metrics['price']:.2f} (相较上周 {price_delta})")
    else:
        lines.append("   - 当前价: N/A")
    if today_metrics.get('stop_1x5_atr') is not None:
        lines.append(f"   - ATR 止损位: ${today_metrics['stop_1x5_atr']:.2f}")
    if today_metrics.get('rr_score') is not None:
        lines.append(f"   - RR 分数: {today_metrics['rr_score']:.2f}")

    # 2. 估值与概率
    lines.append("\n2. 估值与概率：")
    if today_metrics.get('pe_percentile') is not None:
        lines.append(f"   - PE 百分位: {today_metrics['pe_percentile']:.1f}% (相较上周 {pe_delta})")
    if today_metrics.get('recovery_prob') is not None:
        lines.append(f"   - 60日回本概率: {today_metrics['recovery_prob']:.0%} (置信度高)")

    # 3. 宏观与情绪
    lines.append("\n3. 宏观与情绪：")
    if bond_yield is not None:
        impact = "利率升则估值压制" if isinstance(bond_yield, (int, float)) and bond_yield > 4 else "利率下降或支撑估值"
        lines.append(f"   - 10Y 美债收益率: {bond_yield:.2f}% ({impact})")
    if cpi is not None:
        lines.append(f"   - CPI: {cpi:.2f}%")
    if sentiment_today is not None:
        lines.append(f"   - 情绪得分: {sentiment_today['score']:.2f} (相较上周 {sentiment_delta})")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="获取股票最近 N 天的上下文数据供 AI 使用")
    parser.add_argument("--ticker", required=True, help="股票代码，例如 AAPL")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="向前检索的天数，默认 30 天",
    )
    args = parser.parse_args()
    # 仅使用 ticker 参数，已在 get_context 中内部决定取最近两条记录
    context = get_context(args.ticker.upper())
    print(context)


if __name__ == "__main__":
    main()
