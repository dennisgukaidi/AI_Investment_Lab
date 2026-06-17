# rules — AI 可执行操作手册

目的：为 AI 助手（Cline）提供一份可直接执行的、步骤化操作手册。任何自动化操作必须严格按本文件中的"触发词 → 步骤"执行，发生异常时按"故障处理"流程上报。

## 前提与约束
- 运行前激活虚拟环境：Windows: `.venv\Scripts\activate`
- Python 版本应为 3.8+
- 禁止直接手动编辑持仓数据（由 `scripts/portfolio_pipeline.py` 自动维护 `data/holdings/holdings.json`）
- 数据库核心表：`ticker_metrics`（36 列，复合主键 Date+Ticker），由流水线自动维护

## 核心数据流水线（两阶段架构）

### 阶段 A — 基础数据收集 + 入库（步骤 1-6）
### 阶段 B — 精算回填 + 表格导出 + 策略报告（步骤 7-9）

### 触发词 1: "更新数据"
默认根据 `data/watchlist.csv` 清单中的 TICKER 执行完整更新：

```bash
# 步骤 1 — 下载行情（支持 TWS 优先，自动回退 yfinance）
python scripts/portfolio_pipeline.py
# 输出: data/raw/{TICKER}_180d.csv（含 OHLCV + IV + 分析师数据）

# 步骤 2 — 更新新闻与情绪
python scripts/news_collector.py
# 输出: data/news/{TICKER}_news.json（含 sentiment_polarity）

# 步骤 3 — 更新基本面数据（含 forwardPE，供 PEG 通道使用）
python -c "import sys; sys.path.insert(0,'scripts'); from fundamental_data_collector import collect_fundamental_data, save_fundamental_data; from strategy_radar import _read_watchlist_symbols; [save_fundamental_data(collect_fundamental_data(t), t) for t in _read_watchlist_symbols()]"
# 输出: data/fundamentals/{TICKER}_fundamentals.json
# 说明: 一行批量更新 watchlist 中全部标的

# 步骤 4 — 更新宏观经济数据（内置 FRED API key，直接运行即可）
python scripts/macroeconomic_data_collector.py
# 输出: data/macroeconomic/macro_data.json
python scripts/alternative_data_collector.py  
# 输出: Google Trends data/alternative/{TICKER}_alternative.json

# 步骤 5 — 量化分析计算
python scripts/analyze_report.py --all
# 输出: data/analysis/{TICKER}_metrics.json
# 说明: --all 模式下不自动入库，需执行步骤 6

# 步骤 6 — 批量导入数据库 + 同步写入 ticker_metrics 表（36 列）
python scripts/import_analysis_to_db.py
# 写入: quantitative, fundamentals, sentiment, macro 四张基础表
#       ticker_metrics 表（从 quantitative 二次计算，INSERT OR REPLACE）

# 步骤 7 — 精算回填（UPDATE ticker_metrics 表的精算字段）
python scripts/strategy_advisor.py
# 默认遍历 watchlist 全部 ticker，静默写入 DB（不打印报告）
# 精算字段: Crowding_Label, Kelly_Pct, Max_Corr_R2, Breakeven_Days
# 可选参数: --ticker <TICKER> 单只模式
#          --verbose 打印完整报告文本
#          --output reports 同时保存 Markdown 报告
#          --no-db-write 跳过 DB 写入

# 步骤 8 — 导出 CSV 表格（直接从 ticker_metrics 表读取）
python scripts/export_to_csv.py
# 输出: data/ticker_data.csv（36 列，排序: Ticker ASC → Date DESC）

# 步骤 9 — 策略扫描雷达（五模块量化监控，生成 Markdown 报告）
python scripts/strategy_radar.py --verbose
# 输出: output/strategy_radar_YYYYMMDD_HHMM.md
# 说明: 依赖 ticker_metrics 表数据，须在前 8 步全部成功后执行
# 模块: 大盘状态 / 左侧信号 / 基本面筛选 / 持仓追踪 / 出场信号
```

### 回填历史数据

当需要全量刷新数据库时（例如 schema 变更后），直接重跑步骤 6-9：

```bash
# 从 data/analysis/*_metrics.json 重新导入全部历史
python scripts/import_analysis_to_db.py

# 遍历 quantitative 表全部记录，精算 UPDATE
python scripts/strategy_advisor.py

# 导出 CSV
python scripts/export_to_csv.py

# 策略扫描
python scripts/strategy_radar.py --verbose
```

### 触发词 2: "策略回测 YYYY-MM-DD"

```bash
# 历史回测（强制牛市模式，绕过宏观熔断）
python scripts/strategy_radar.py --as-of YYYY-MM-DD --force-bull

# 选项
#   --tickers TSLA,AAPL    指定标的（默认 watchlist.csv 全量）
#   --force-bull           绕过宏观熔断器（仅限回测）
#   --print                控制台打印完整报告
#   --verbose              打印模块处理日志
#   报告输出到 output/strategy_radar_asof_YYYYMMDD_bull.md
```

### 触发词 3: "策略扫描"（实时）

```bash
# 实时四模块策略扫描
python scripts/strategy_radar.py --verbose
```

---

注意：为防止分析/入库/生成报告使用不完整数据，**步骤 5-7 必须在步骤 1-4（行情、新闻、基本面、宏观/替代）全部成功并生成相应数据文件后执行**。
推荐使用仓库中新添加的调度脚本 `scripts/run_full_pipeline.py` 来自动化此检查与执行：
```bash
# 在项目根目录运行（会按 `data/watchlist.csv` 遍历并校验各类数据文件）：
python scripts/run_full_pipeline.py
```

