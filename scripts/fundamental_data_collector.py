#!/usr/bin/env python3
"""
Fundamental Data Collector - 基本面估值数据收集器

此脚本收集股票的基本面估值数据，包括：
- 动态P/E、P/B、P/S比率
- EPS增长率、营收预期
- 财务健康指标（债务比率、现金流）
- 行业比较数据

数据来源：
- yfinance: 财务比率和基本面数据
- 存储格式: data/fundamentals/{ticker}_fundamentals.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    sys.stderr.write("Required packages: pip install yfinance pandas\n")
    sys.exit(1)

# --------------------------------------------------------------------------- 
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[1]
FUNDAMENTALS_DIR = BASE_DIR / "data" / "fundamentals"
FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)


def calculate_valuation_ratios(ticker: str) -> Dict[str, Any]:
    """计算估值比率和历史分位数"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        # 当前估值比率
        pe_ratio = info.get('trailingPE')
        forward_pe = info.get('forwardPE')
        pb_ratio = info.get('priceToBook')
        ps_ratio = info.get('priceToSalesTrailing12Months')
        
        # 财务健康指标
        debt_to_equity = info.get('debtToEquity')
        current_ratio = info.get('currentRatio')
        roe = info.get('returnOnEquity')
        roa = info.get('returnOnAssets')
        
        # 增长指标
        eps_growth = info.get('earningsQuarterlyGrowth')
        revenue_growth = info.get('revenueQuarterlyGrowth')
        
        # 市值和行业
        market_cap = info.get('marketCap')
        industry = info.get('industry')
        sector = info.get('sector')
        
        return {
            "valuation_ratios": {
                "pe_ratio": pe_ratio,
                "forward_pe": forward_pe,
                "pb_ratio": pb_ratio,
                "ps_ratio": ps_ratio,
            },
            "financial_health": {
                "debt_to_equity": debt_to_equity,
                "current_ratio": current_ratio,
                "roe": roe,
                "roa": roa,
            },
            "growth_indicators": {
                "eps_growth": eps_growth,
                "revenue_growth": revenue_growth,
            },
            "company_info": {
                "market_cap": market_cap,
                "industry": industry,
                "sector": sector,
            }
        }
        
    except Exception as e:
        return {"error": str(e)}


def get_historical_valuation_percentiles(ticker: str) -> Dict[str, Any]:
    """计算当前估值在历史分位数中的位置"""
    try:
        # 获取历史数据（简化版，实际需要更复杂的计算）
        stock = yf.Ticker(ticker)
        
        # 这里可以扩展为计算历史分位数
        # 由于yfinance限制，我们返回基础数据
        return {
            "pe_percentile": None,  # 需要历史数据计算
            "pb_percentile": None,
            "note": "Historical percentiles require extended market data"
        }
        
    except Exception as e:
        return {"error": str(e)}


def collect_fundamental_data(ticker: str) -> Dict[str, Any]:
    """收集完整的根本面数据"""
    current_ratios = calculate_valuation_ratios(ticker)
    historical_percentiles = get_historical_valuation_percentiles(ticker)
    
    data = {
        "ticker": ticker,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "current_ratios": current_ratios,
        "historical_percentiles": historical_percentiles,
    }
    
    return data


def save_fundamental_data(data: Dict[str, Any], ticker: str) -> Path:
    """保存基本面数据到JSON文件"""
    output_path = FUNDAMENTALS_DIR / f"{ticker}_fundamentals.json"
    output_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    return output_path


def main(ticker: str = "TSLA") -> None:
    """主函数：收集并保存基本面数据"""
    print(f"Collecting fundamental data for {ticker}...")
    
    data = collect_fundamental_data(ticker)
    output_path = save_fundamental_data(data, ticker)
    
    print(f"[OK] Fundamental data saved to {output_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="收集股票基本面估值数据")
    parser.add_argument("ticker", help="股票代码")
    args = parser.parse_args()
    
    main(args.ticker)