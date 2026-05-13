#!/usr/bin/env python3
"""
Macroeconomic Data Collector - 宏观经济数据收集器

此脚本收集宏观经济指标，包括：
- 利率数据：美联储基金利率、10年期国债收益率
- 通胀指标：CPI、PPI数据
- 就业数据：非农就业、失业率
- GDP相关：季度GDP增长、PMI指数

数据来源：
- FRED API (Federal Reserve Economic Data)
- 存储格式: data/macroeconomic/macro_data.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any

try:
    from fredapi import Fred
    import pandas as pd
except ImportError:
    sys.stderr.write("Required packages: pip install fredapi pandas\n")
    sys.exit(1)

# --------------------------------------------------------------------------- 
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[1]
MACRO_DIR = BASE_DIR / "data" / "macroeconomic"
MACRO_DIR.mkdir(parents=True, exist_ok=True)

# FRED API Key - 用户需要设置
FRED_API_KEY = "your_fred_api_key_here"  # 需要用户替换为实际API密钥

# FRED系列ID映射
FRED_SERIES = {
    # 利率数据
    "fed_funds_rate": "FEDFUNDS",  # 联邦基金利率
    "ten_year_treasury": "GS10",   # 10年期国债收益率
    
    # 通胀指标
    "cpi": "CPIAUCSL",             # 消费者物价指数
    "ppi": "PPIACO",               # 生产者物价指数
    
    # 就业数据
    "nonfarm_payroll": "PAYEMS",   # 非农就业人数
    "unemployment_rate": "UNRATE", # 失业率
    
    # GDP相关
    "gdp_quarterly": "GDP",        # 季度GDP
    "ism_pmi": "NAPM",             # ISM制造业PMI
}


def get_fred_data(series_id: str, api_key: str, months: int = 24) -> Dict[str, Any]:
    """从FRED获取指定系列的最新数据"""
    try:
        fred = Fred(api_key=api_key)
        
        # 获取最近N个月的数据
        end_date = datetime.now()
        start_date = end_date - timedelta(days=months*30)
        
        data = fred.get_series(series_id, start_date, end_date)
        
        if data.empty:
            return {"error": f"No data available for {series_id}"}
        
        # 转换为字典格式
        values = data.dropna()
        latest_value = values.iloc[-1] if not values.empty else None
        latest_date = values.index[-1] if not values.empty else None
        
        return {
            "latest_value": float(latest_value) if latest_value is not None else None,
            "latest_date": latest_date.isoformat() if latest_date else None,
            "series": values.to_dict(),
            "unit": "percent" if "RATE" in series_id or "CPI" in series_id or "PPI" in series_id else "index"
        }
        
    except Exception as e:
        return {"error": str(e)}


def collect_macroeconomic_data(api_key: str) -> Dict[str, Any]:
    """收集所有宏观经济指标"""
    data = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "indicators": {}
    }
    
    for name, series_id in FRED_SERIES.items():
        print(f"Fetching {name} ({series_id})...")
        indicator_data = get_fred_data(series_id, api_key)
        data["indicators"][name] = indicator_data
        
        # 添加延迟避免API限制
        import time
        time.sleep(0.5)
    
    return data


def save_macroeconomic_data(data: Dict[str, Any]) -> Path:
    """保存宏观经济数据"""
    output_path = MACRO_DIR / "macro_data.json"
    output_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8"
    )
    return output_path


def main(api_key: str = FRED_API_KEY) -> None:
    """主函数"""
    if api_key == "your_fred_api_key_here":
        print("❌ 请设置FRED API密钥")
        print("   1. 访问 https://fred.stlouisfed.org/docs/api/api_key.html 获取API密钥")
        print("   2. 修改脚本中的 FRED_API_KEY 变量")
        return
    
    print("Collecting macroeconomic data...")
    
    data = collect_macroeconomic_data(api_key)
    output_path = save_macroeconomic_data(data)
    
    print(f"✅ Macroeconomic data saved to {output_path}")
    
    # 显示最新数据摘要
    indicators = data.get("indicators", {})
    print("\n📊 最新宏观经济数据摘要:")
    for name, info in indicators.items():
        if "error" not in info:
            value = info.get("latest_value")
            date = info.get("latest_date")
            if value is not None:
                print(f"  {name}: {value:.2f} ({date})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="收集宏观经济数据")
    parser.add_argument("--api-key", help="FRED API密钥", default=FRED_API_KEY)
    args = parser.parse_args()
    
    main(args.api_key)