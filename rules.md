# rules — AI 可执行操作手册

目的：为 AI 助手（Cline）提供一份可直接执行的、步骤化操作手册。任何自动化操作必须严格按本文件中的"触发词 → 步骤"执行，发生异常时按"故障处理"流程上报。

## 前提与约束
- 运行前激活虚拟环境：Windows: `.venv\Scripts\activate`
- Python 版本应为 3.8+
- 禁止直接手动编辑持仓数据（由 `scripts/portfolio_pipeline.py` 自动维护 `data/holdings/holdings.json`）

## 核心数据流水线

### 触发词 1: "更新数据"
默认根据 `data/watchlist.csv` 清单中的 TICKER 执行完整更新：

```bash
# 步骤 1 — 下载行情（支持 TWS 优先，自动回退 yfinance）
python scripts/portfolio_pipeline.py
# 输出: data/raw/{TICKER}_180d.csv（含 OHLCV + IV + 分析师数据）

# 步骤 2 — 更新新闻与情绪
python scripts/news_collector.py
# 输出: data/news/{TICKER}_news.json（含 sentiment_polarity）

# 步骤 3 — 更新基本面数据
python scripts/fundamental_data_collector.py <TICKER>
# 输出: data/fundamentals/{TICKER}_fundamentals.json
# 注意: 可手动循环执行 watchlist 内所有 TICKER，或逐个执行

# 步骤 4 — 更新宏观经济数据（内置 FRED API key，直接运行即可）
python scripts/macroeconomic_data_collector.py
# 输出: data/macroeconomic/macro_data.json
python alternative_data_collector.py  
# 输出: Google Trends data/alternative/{TICKER}_alternative.json

# 步骤 5 — 量化分析计算
python scripts/analyze_report.py --all
# 输出: data/analysis/{TICKER}_metrics.json
# 说明: --all 模式下不自动入库，需执行步骤 6

# 步骤 6 — 批量导入数据库（将 metrics + fundamentals + sentiment + macro 写入 DB）
python scripts/import_analysis_to_db.py

# 步骤 7 — 生成策略报告
python scripts/strategy_advisor.py 
# 输出: reports/strategy_{TICKER}_{YYYYMMDD}.md
#       reports/holdings_summary_{YYYYMMDD}.md

# 步骤 8 — 更新表格
python scripts/export_to_csv.py
# 数据报告整合归档


注意：为防止分析/入库/生成报告使用不完整数据，**步骤 5-7 必须在步骤 1-4（行情、新闻、基本面、宏观/替代）全部成功并生成相应数据文件后执行**。
推荐使用仓库中新添加的调度脚本 `scripts/run_full_pipeline.py` 来自动化此检查与执行：
```bash
# 在项目根目录运行（会按 `data/watchlist.csv` 遍历并校验各类数据文件）：
python scripts/run_full_pipeline.py
```
```
python scripts/organize_reports.py  整理reports文件夹按ticker归类

# 检查依赖
python -c "import ib_insync; print('ib_insync ok')"
python -c "import yfinance; print('yfinance ok')"
python -c "import pandas; print('pandas ok')"
python -c "import numpy; print('numpy ok')"
python -c "import sqlite3; print('sqlite3 ok')"

# 检查数据库完整性
python -c "import sqlite3; conn = sqlite3.connect('investment_lab.db'); cur = conn.cursor()
for t in ['quantitative','fundamentals','sentiment','macro']:
    try:
        cur.execute(f'SELECT COUNT(*) FROM {t}'); print(f'{t}: {cur.fetchone()[0]} 条记录')
    except: print(f'{t}: 不存在')
conn.close()"

# 检查关键数据文件最近修改时间
python -c "
import pathlib; from datetime import datetime
for p in sorted(pathlib.Path('data/raw').glob('*_180d.csv')):
    mtime = datetime.fromtimestamp(p.stat().st_mtime)
    if (datetime.now() - mtime).days < 1: print(f'[OK] {p.name}: {mtime}')
"
```
