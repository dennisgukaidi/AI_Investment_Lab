#!/usr/bin/env python3
"""
Alternative Data Collector - 替代数据收集器

此脚本收集非传统数据源，包括：
- Google Trends：股票搜索热度
- 社交媒体情绪：Twitter/Reddit情绪分析（简化版）
- 新闻情绪量化：基于现有新闻数据的情感分析

数据来源：
- Google Trends API (pytrends)
- 社交媒体API（需要额外配置）
- 存储格式: data/alternative/{ticker}_alternative.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List

try:
    from pytrends.request import TrendReq
    import pandas as pd
    import requests
except ImportError:
    sys.stderr.write("Required packages: pip install pytrends pandas requests\n")
    sys.exit(1)

# --------------------------------------------------------------------------- 
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[1]
ALTERNATIVE_DIR = BASE_DIR / "data" / "alternative"
ALTERNATIVE_DIR.mkdir(parents=True, exist_ok=True)


def get_google_trends_data(keyword: str, months: int = 12) -> Dict[str, Any]:
    """获取Google Trends搜索热度数据"""
    try:
        pytrends = TrendReq(hl='en-US', tz=360)
        
        # 设置时间范围
        timeframe = f'today {months}-m'
        
        # 构建关键词列表
        kw_list = [keyword]
        
        # 获取趋势数据
        pytrends.build_payload(kw_list, cat=0, timeframe=timeframe, geo='', gprop='')
        interest_over_time_df = pytrends.interest_over_time()
        
        if interest_over_time_df.empty:
            return {"error": f"No Google Trends data for {keyword}"}
        
        # 处理数据
        data = interest_over_time_df[keyword].dropna()
        
        # 计算统计指标
        latest_value = data.iloc[-1] if not data.empty else 0
        avg_value = data.mean()
        max_value = data.max()
        trend_direction = "increasing" if data.iloc[-1] > data.iloc[-5:].mean() else "decreasing"
        
        return {
            "latest_search_volume": int(latest_value),
            "average_volume": float(avg_value),
            "max_volume": int(max_value),
            "trend_direction": trend_direction,
            "time_series": {str(k): int(v) for k, v in data.to_dict().items()},
            "period_months": months
        }
        
    except Exception as e:
        return {"error": str(e)}


def get_reddit_sentiment(ticker: str) -> Dict[str, Any]:
    """获取Reddit情绪数据（简化版，使用Pushshift API）"""
    try:
        # 注意：这是一个简化的实现
        # 实际部署需要考虑API限制和数据质量
        
        # 这里使用一个简化的情绪评分
        # 实际应该调用Reddit API或使用专门的情绪分析服务
        
        return {
            "sentiment_score": 0.0,  # -1到1之间的分数
            "mention_count": 0,
            "top_posts": [],
            "note": "Reddit sentiment analysis requires API configuration",
            "last_updated": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        return {"error": str(e)}


def get_twitter_sentiment(ticker: str) -> Dict[str, Any]:
    """获取Twitter情绪数据（简化版）"""
    try:
        # 类似Reddit，这里也需要API配置
        return {
            "sentiment_score": 0.0,
            "tweet_count": 0,
            "positive_ratio": 0.0,
            "note": "Twitter sentiment analysis requires API configuration",
            "last_updated": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        return {"error": str(e)}


def aggregate_news_sentiment(ticker: str) -> Dict[str, Any]:
    """聚合现有新闻数据的情感分析"""
    try:
        news_file = BASE_DIR / "data" / "news" / f"{ticker}_news.json"
        
        if not news_file.exists():
            return {"error": "News file not found"}
        
        with open(news_file, 'r', encoding='utf-8') as f:
            news_data = json.load(f)
        
        if not news_data:
            return {"average_sentiment": 0.0, "news_count": 0}
        
        # 计算平均情感分数
        sentiments = []
        for item in news_data:
            polarity = item.get("sentiment_polarity", 0)
            subjectivity = item.get("sentiment_subjectivity", 0)
            if polarity is not None:
                sentiments.append(polarity)
        
        avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
        
        # 分类情绪
        if avg_sentiment > 0.1:
            sentiment_category = "positive"
        elif avg_sentiment < -0.1:
            sentiment_category = "negative"
        else:
            sentiment_category = "neutral"
        
        return {
            "average_sentiment": round(avg_sentiment, 3),
            "sentiment_category": sentiment_category,
            "news_count": len(sentiments),
            "sentiment_distribution": {
                "positive": len([s for s in sentiments if s > 0.1]),
                "negative": len([s for s in sentiments if s < -0.1]),
                "neutral": len([s for s in sentiments if -0.1 <= s <= 0.1])
            }
        }
        
    except Exception as e:
        return {"error": str(e)}


def collect_alternative_data(ticker: str) -> Dict[str, Any]:
    """收集所有替代数据"""
    print(f"Collecting alternative data for {ticker}...")
    
    # Google Trends（使用股票代码作为关键词）
    trends_data = get_google_trends_data(ticker)
    
    # 社交媒体情绪
    reddit_data = get_reddit_sentiment(ticker)
    twitter_data = get_twitter_sentiment(ticker)
    
    # 新闻情绪聚合
    news_sentiment = aggregate_news_sentiment(ticker)
    
    data = {
        "ticker": ticker,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "google_trends": trends_data,
        "social_media": {
            "reddit": reddit_data,
            "twitter": twitter_data
        },
        "news_sentiment_aggregate": news_sentiment
    }
    
    return data


def save_alternative_data(data: Dict[str, Any], ticker: str) -> Path:
    """保存替代数据"""
    # 处理pandas Timestamp对象
    def convert_timestamps(obj):
        if isinstance(obj, dict):
            return {k: convert_timestamps(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_timestamps(item) for item in obj]
        elif hasattr(obj, 'isoformat'):  # pandas Timestamp or datetime
            return obj.isoformat()
        else:
            return obj
    
    processed_data = convert_timestamps(data)
    
    output_path = ALTERNATIVE_DIR / f"{ticker}_alternative.json"
    output_path.write_text(
        json.dumps(processed_data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    return output_path


def main(ticker: str = "TSLA") -> None:
    """主函数"""
    print(f"Collecting alternative data for {ticker}...")
    
    data = collect_alternative_data(ticker)
    output_path = save_alternative_data(data, ticker)
    
    print(f"✅ Alternative data saved to {output_path}")
    
    # 显示摘要
    trends = data.get("google_trends", {})
    news_sent = data.get("news_sentiment_aggregate", {})
    
    print("\n📊 替代数据摘要:")
    if "error" not in trends:
        print(f"  Google Trends: {trends.get('latest_search_volume', 'N/A')} (趋势: {trends.get('trend_direction', 'N/A')})")
    
    if "error" not in news_sent:
        print(f"  新闻情绪: {news_sent.get('average_sentiment', 'N/A')} ({news_sent.get('sentiment_category', 'N/A')})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="收集替代数据")
    parser.add_argument("ticker", help="股票代码")
    args = parser.parse_args()
    
    main(args.ticker)