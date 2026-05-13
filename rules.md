# 投资原则与持仓记录 (v4.5 定稿版)

## 📊 实时持仓看板
> **注意**：本表格由 `portfolio_pipeline.py` 自动通过 TWS 实盘同步，禁止手动修改。

| 股票代码 | 持仓股数 | 持仓成本 | 市价 | 每股盈亏 | 备注 |
|---|---|---|---|---|---|
| CEG | 10.0 | $289.61 | $292.49 | $2.88 |  |
| IBKR | 6.144 | $65.10 | $84.58 | $19.48 |  |
| TSLA | 20.0 | $390.55 | $430.33 | $39.78 |  |
| VST | 20.0 | $166.05 | $146.77 | $-19.28 |  |
## 🎯 核心投资原则
1. **健康第一**：不熬夜看盘，不进行情绪化交易。
2. **稳健增值**：追求长期跑赢通胀，不追求高频短线。
3. **数据驱动**：所有操作逻辑必须经过 180 天量价数据与 14 天舆情分析。

## ⚙️ 自动化工作流 (Standard Operating Procedure)
当用户输入以下“触发词”时，Cline 必须严格执行对应脚本：

1. **“更新数据”**：
   - 运行 `scripts/portfolio_pipeline.py`：同步 TWS 实盘到本文件，补齐 180d 历史数据。
   - 运行 `scripts/news_collector.py`：抓取最近 14 天新闻摘要。
   
2. **“分析股市”或“分析 [Ticker]”**：
1. **触发脚本**：运行 `python scripts/analyze_report.py [Ticker]`。
2. **读取结论**：解析 `data/analysis/[Ticker]_metrics.json`。
3. **撰写内参**：Cline 必须基于 JSON 里的硬核数据（如 Monte Carlo 概率、IV 分位、RR 评分），结合自己的理解写出一份有深度、有人性的研报。

3. **“自检”**：
   - 检查 7496 端口连接及 `data/` 目录下文件的修改日期。
   - 当需要更新脚本或需要写新脚本去查询 cline_rules.md

## 📋 详细项目运行流程

### 整体运行逻辑
本项目基于数据驱动的投资分析系统，通过自动化脚本收集市场数据、新闻舆情和持仓信息，然后使用量化算法进行风险评估和趋势分析，最终生成投资报告。系统遵循“动静分离”原则，数据存储在 `data/` 目录，脚本逻辑在 `scripts/` 目录，报告输出在 `reports/` 目录。

### 核心组件
- **数据源**：Interactive Brokers TWS API（主） + yfinance（备用）
- **分析引擎**：基于历史波动率（HV）、隐含波动率（IV）、蒙特卡洛模拟、引导分析等量化方法
- **风控机制**：动态止损（基于ATR）、概率分布评估
- **输出格式**：JSON metrics + Markdown 研报

### 详细运行流程
1. **环境准备**：
   - 确保 TWS 或 IB Gateway 运行在端口 7496，客户端ID 10
   - 激活 Python 虚拟环境（.venv）
   - 检查依赖：ib_insync, pandas, numpy, yfinance 等

2. **数据更新阶段**：
   - 执行 `scripts/portfolio_pipeline.py`：
     - 连接 TWS，同步实盘持仓到 `rules.md`
     - 下载观察清单（watchlist.csv）+ SPY 的 500 日历日 OHLCV 数据
     - 丰富数据：计算滚动 HV 填充 IV，使用 yfinance 获取分析师数据
     - 输出：`data/raw/{ticker}_ohlcv.csv`
   - 执行 `scripts/news_collector.py`：
     - 从 yfinance 获取最近 30 天新闻
     - 集成 TextBlob 情绪分析（polarity + subjectivity）
     - 增量更新，保留最多 50 条或 14 天内新闻
     - 输出：`data/news/{ticker}_news.json`（含情绪评分）
   - 执行 `scripts/fundamental_data_collector.py [Ticker]`：
     - 收集基本面估值数据：P/E、P/B、P/S 比率
     - 财务健康指标：债务比率、流动比率、ROE/ROA
     - 增长指标：EPS 增长率、营收增长、市值信息
     - 输出：`data/fundamentals/{ticker}_fundamentals.json`
   - 执行 `scripts/alternative_data_collector.py [Ticker]`：
     - Google Trends 搜索热度数据（12 个月历史）
     - 新闻情绪聚合分析（基于 news_collector.py 数据）
     - 输出：`data/alternative/{ticker}_alternative.json`
   - 执行 `scripts/macroeconomic_data_collector.py --api-key [FRED_KEY]`：
     - 联邦基金利率、10 年期国债收益率
     - CPI、PPI 通胀数据
     - 非农就业、失业率等就业数据
     - GDP 季度数据、ISM PMI 指数
     - 输出：`data/macroeconomic/macro_data.json`

3. **分析阶段**：
   - 执行 `python scripts/analyze_report.py [Ticker]`：
     - 输入：`data/raw/{ticker}_ohlcv.csv`, `data/news/{ticker}_news.json`, `rules.md`
     - 算法执行：
       - 蒙特卡洛模拟（5000 次，基于最新 IV）
       - IV 分位计算（180 日）
       - 风险矩阵（ATR 止损）
       - 趋势分析（MA20/60/200）
       - 引导分析（历史收益抽样）
       - 大盘对比（SPY 趋势、相关性）
     - 输出：`data/analysis/{ticker}_metrics.json`

4. **报告生成阶段**：
   - 基于 metrics.json 生成 Markdown 研报
   - 结合基本面、宏观、情绪等多维度数据
   - 结合持仓成本计算回本概率
   - 强调核心结论（概率、IV 分位、抄底信号等）

### 需要执行的文件列表
- **核心脚本**：
  - `scripts/portfolio_pipeline.py`：投资组合流水线（持仓同步 + 历史数据下载）
  - `scripts/news_collector.py`：新闻收集器（含情绪分析）
  - `scripts/analyze_report.py`：量化分析引擎
  - `scripts/fundamental_data_collector.py`：基本面数据收集器
  - `scripts/alternative_data_collector.py`：替代数据收集器（Google Trends + 情绪）
  - `scripts/macroeconomic_data_collector.py`：宏观经济数据收集器（FRED API）
- **配置文件**：
  - `data/watchlist.csv`：观察股票清单
  - `rules.md`：持仓和原则（自动更新）
- **输出文件**：
  - `data/raw/{ticker}_ohlcv.csv`：历史价格数据
  - `data/news/{ticker}_news.json`：新闻数据（含情绪评分）
  - `data/fundamentals/{ticker}_fundamentals.json`：基本面估值数据
  - `data/alternative/{ticker}_alternative.json`：替代数据（Google Trends + 情绪）
  - `data/macroeconomic/macro_data.json`：宏观经济指标
  - `data/analysis/{ticker}_metrics.json`：分析结果
  - `reports/{ticker}_report.md`：投资报告

### 运行出现问题或需要修改脚本的处理
如果运行过程中出现错误、需要调整参数或编写新脚本，请参考 `agent_rules.md` 文件，该文件包含机器人更新脚本的详细规则和参数设置指南。所有脚本修改必须遵循 `cline_rules.md` 中的开发规范。

## ✅ 验证状态

已对项目中的所有 Python 脚本进行语法编译检查，未发现错误。以下文件已成功编译：

```
scripts/alternative_data_collector.py
scripts/analyze_report.py
scripts/download_watchlist_data.py
scripts/fundamental_data_collector.py
scripts/macroeconomic_data_collector.py
scripts/news_collector.py
scripts/portfolio_pipeline.py
```

文档中引用的脚本路径、文件名称均与实际文件保持一致，确保新手能够顺畅地按照说明进行操作。

*最后更新：2026年5月13日*