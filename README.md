# AI_Investment_Lab — 项目说明与详细文件结构

一句话：基于 Python 的美股投研流水线，整合 TWS/yfinance、新闻舆情与量化分析，输出结构化 metrics 与人性化研报。

快速上手（最小步骤）
- 克隆并进入项目：
  - `git clone <repo>`
  - `cd AI_Investment_Lab`
- 创建并激活虚拟环境（Windows）：
  - `python -m venv .venv`
  - `.venv\Scripts\activate`
- 安装依赖：
  - `pip install ib_insync pandas numpy yfinance textblob pytrends fredapi`

常用命令
- 更新数据流水线：
  - `python scripts/portfolio_pipeline.py`
  - `python scripts/news_collector.py`
- 分析单只股票并自动入库：
  - `python scripts/analyze_report.py <TICKER>`
- 批量入库并验证：
  - `python scripts/ingest_all_and_show_tsla.py`
- 构建 AI 上下文：
  - `python scripts/get_context_for_ai.py --ticker <TICKER> > reports/ai_<TICKER>.txt`

详细文件与目录说明
- `data/`
  - `raw/`：历史行情 CSV，命名格式 `{TICKER}_180d.csv`，包含 OHLCV、IV 与占位的分析师字段。
  - `news/`：新闻与情绪 JSON，格式 `{TICKER}_news.json`，包含标题、摘要、发布时间与情绪分值。
  - `fundamentals/`：基本面 JSON，`{TICKER}_fundamentals.json`，含估值、财报摘要与关键比率。
  - `alternative/`：替代数据 JSON（如 Google Trends、聚合舆情），格式 `{TICKER}_alternative.json`。
  - `analysis/`：分析结果 JSON，`{TICKER}_metrics.json`，含概率分布、风险指标、趋势与 summary 字段。
  - `macroeconomic/`：宏观数据文件 `macro_data.json`，包含 FRED 指标（利率、CPI、GDP 等）。
  - `watchlist.csv`：观察清单，逗号分隔的股票代码。

- `scripts/`（核心脚本）
  - `portfolio_pipeline.py`：完整流水线脚本。功能：连接 TWS（优先）或回退至 yfinance，同步持仓、下载历史行情、更新 `data/raw/`，并可生成持仓快照到 `rules.md`。
    - 典型用法：`python scripts/portfolio_pipeline.py`
  - `download_watchlist_data.py`：轻量测试脚本。默认只处理 `watchlist.csv` 的第一个 ticker，并在无法连接 TWS 时回退到 yfinance。适合快速调试或 CI 快速检查。
    - 建议：保留作为测试工具或扩展为 `--all/--tickers` 参数。
  - `news_collector.py`：抓取新闻并做情绪分析（TextBlob），输出到 `data/news/`。
  - `fundamental_data_collector.py`：抓取/计算基本面指标并保存到 `data/fundamentals/`。
  - `alternative_data_collector.py`：收集 Google Trends 与其他替代数据，输出到 `data/alternative/`。
  - `macroeconomic_data_collector.py`：通过 FRED API 下载宏观指标，保存到 `data/macroeconomic/macro_data.json`（需要 `--api-key`）。
  - `analyze_report.py`：量化分析引擎。输入：`data/raw/`、`data/news/`、`data/fundamentals/` 等，输出：`data/analysis/{TICKER}_metrics.json`，并可生成 `reports/{TICKER}_report.md`。
    - 内置行为：保存 metrics 后会调用 `_db_ingest_helper.ingest_all()`（若可用）将数据写入 `investment_lab.db`。
  - `_db_ingest_helper.py`：数据库入库助手，封装将 JSON 写入 SQLite 的逻辑。
  - `ingest_all_and_show_tsla.py`：遍历 `data/analysis/` 入库并打印 TSLA 验证信息，常用于验收。
  - `get_context_for_ai.py`：从 DB 或文件提取最近 30 天上下文并输出自然语言文本，方便将数据喂给大模型。
  - `init_db.py`：初始化 `investment_lab.db` 的表结构（`fundamentals`、`quantitative`、`macro`、`sentiment`），并提供 `--clean` 清理旧数据。应在首次使用库功能前运行。
  - `import_analysis_to_db.py`：备选的批量导入工具（若不依赖自动入库，可用此脚本导入历史 metrics）。
  - `strategy_advisor.py`：基于多维度数据（量化+基本面+情绪）生成策略建议与 Markdown 报告。
  - `strategy_advisor.py`：基于多维度数据（量化+基本面+情绪）生成策略建议与 Markdown 报告。
    - 输入来源：优先从 `investment_lab.db`（`quantitative` 表）读取最新 `metrics`，也可通过 `--cost` 覆盖成本价。
    - 输出：默认打印到 `stdout`，可使用 `--output reports/strategy_<TICKER>.md` 将 Markdown 报告保存至 `reports/`。
    - 典型用法：
      - 单只：`python scripts/strategy_advisor.py --ticker TSLA --output reports/strategy_TSLA.md`
      - 多只（逗号分隔）：`python scripts/strategy_advisor.py --tickers AAPL,GOOG --output reports/strategy_AAPL.md` （注意：`--output` 为单文件路径，建议为每只单独运行以避免覆盖）
    - 报告结构：标题、价格与动能、回本概率（10/20/60d）、风险对冲位、估值与安全性、宏观情绪、时间维度对比与最终建议。
    - 说明：脚本会将 `quantitative` 表中的 `metrics` JSON 解析为结构化快照并生成易读的 Markdown，适合直接发布或归档。

- `reports/`
  - 存放生成的研报（如 `TSLA_report.md`）、AI 上下文文件（`ai_<TICKER>.txt`）以及执行日志/错误文件（如 `error_<cmd>_<ts>.log`）。

- 根目录文件
  - `rules.md`：AI 可执行的操作手册（触发词 → 明确步骤）。
  - `agent_rules.md` / `cline_rules.md`：团队/Agent 的开发与脚本更新规范。
  - `README.md`：本文件。

运行与故障排查要点（精要）
- TWS/IB Gateway：确保运行并允许 API（端口 `7496`，clientId 可配置）。
- 依赖缺失：脚本通常会在启动时检查依赖并提示 `pip install`。推荐在 `.venv` 中安装依赖并导出 `requirements.txt`。
- 日志与错误：若命令失败，请查看 `reports/` 下是否有对应的 error 日志，并将 stdout/stderr 一并保存以便排查。

我可以继续：1) 将 `download_watchlist_data.py` 扩展为支持 `--all/--tickers` 参数；2) 给每个脚本补充 CLI 示例；3) 为 `init_db.py` 添加 `--recreate` 与 `--dry-run` 功能。请选择要我接着做的项。 

最后更新：2026-05-13