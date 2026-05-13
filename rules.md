# rules — 项目操作手册（供 AI 执行）

目的：为 AI 助手（Cline）提供一份可直接执行的、步骤化操作手册。任何自动化操作必须严格按本文件中的“触发词 → 步骤”执行，发生异常时按“故障处理”流程上报。

前提与约束
- 在运行脚本前请激活虚拟环境：Windows: `.venv\\Scripts\\activate`。
- Python 版本应为 3.8+
- 禁止直接手动编辑持仓表（由 `scripts/portfolio_pipeline.py` 自动维护）。


触发词与明确执行步骤（AI 应按序执行并报告每步结果）

1) "更新数据" — 完整数据流水线
   - 命令：
     - `python scripts/portfolio_pipeline.py`
     - `python scripts/news_collector.py`
     - 可选（批量）：`python scripts/fundamental_data_collector.py <TICKER>`、`python scripts/alternative_data_collector.py <TICKER>`、`python scripts/macroeconomic_data_collector.py --api-key <FRED_KEY>`
   - 预期输出：`data/raw/{ticker}_ohlcv.csv`、`data/news/{ticker}_news.json`、`data/fundamentals/{ticker}_fundamentals.json`、`data/macroeconomic/macro_data.json`
   - 验证：脚本退出码为 0，且对应文件的修改时间更新（检查 `os.path.getmtime`）。

2) "分析 [TICKER]" — 单只股票分析并自动入库
   - 命令：`python scripts/analyze_report.py <TICKER>`
   - 预期输出：`data/analysis/<TICKER>_metrics.json`，`reports/<TICKER>_report.md`（若脚本生成）
   - 自动入库：`analyze_report.py` 会在保存 metrics 后调用 `_db_ingest_helper.ingest_all()`（若启用），将数据写入 `investment_lab.db`。
   - 验证：检查 metrics JSON 存在并包含 `summary` 或 `probability` 字段；若启用入库，验证数据库中存在对应记录。

3) "入库全部" — 批量导入并验证（验收脚本）
   - 命令：`python scripts/ingest_all_and_show_tsla.py`
   - 预期输出：数据库写入日志；控制台打印 TSLA 验证结果。

4) "获取上下文 [TICKER]" — 为大模型构建上下文
   - 命令：`python scripts/get_context_for_ai.py --ticker <TICKER> > reports/ai_<TICKER>.txt`
   - 预期输出：`reports/ai_<TICKER>.txt`（包含最近 30 天的量化、基本面、宏观与舆情摘要）

5) "自检" — 系统健康检查
   - 步骤：检查 TWS/IB Gateway 是否监听端口 7496（或尝试通过 ib_insync 连接）；检查 `data/` 目录关键文件（raw、analysis、macro）最近修改时间；检查虚拟环境依赖是否安装（可通过 `pip list` 校验关键包）。
   - 命令示例（Windows PowerShell）：
     - `netstat -ano | Select-String ":7496"`
     - `python -c "import ib_insync; print('ib_insync ok')"`

故障处理（AI 必须在执行失败时遵循）
- 若脚本非零退出：收集 stderr、stdout 与返回码，保存到 `reports/error_<command>_<timestamp>.log`，并向用户报告执行步骤与异常摘要。
- 若关键输出文件未生成：回滚并提示用户（或在确认下重试一次）。
- 对任何会引发真实交易或改动账户的操作（需与 TWS 交互写入订单），AI 必须先向用户确认并获得明确许可。

脚本与文件说明（核心、简明）
- `scripts/portfolio_pipeline.py` — 持仓同步 + 历史行情下载，更新 `data/raw/` 和 `rules.md`（持仓部分）。
- `scripts/news_collector.py` — 新闻抓取与情绪打分，输出到 `data/news/`。
- `scripts/analyze_report.py` — 量化分析引擎，生成 `data/analysis/*_metrics.json`，并触发自动入库（如配置）。
- `scripts/_db_ingest_helper.py` — 数据入库助手，`ingest_all(ticker, metrics_path)` 将 metrics、fundamentals、macro 写入 `investment_lab.db`。
- `scripts/ingest_all_and_show_tsla.py` — 批量入库并打印 TSLA 验证信息。
- `scripts/get_context_for_ai.py` — 从 DB/文件提取最近 30 天上下文，供大模型使用。
 - `scripts/strategy_advisor.py` — 策略顾问报告生成器（基于 `quantitative` 表的 `metrics`）。
    - 功能：从 `investment_lab.db` 读取最新 `metrics`，解析 bootstrap 回本概率、风险矩阵、技术面与估值分位，并生成结构化的 Markdown 报告。
    - 输出位置：默认打印到 `stdout`，推荐写入 `reports/strategy_<TICKER>.md` 以便归档与审阅。
    - 运行示例：
       - `python scripts/strategy_advisor.py --ticker TSLA --output reports/strategy_TSLA.md`
       - 若需对 watchlist 全部标的生成报告，可在 AI 控制下循环调用（每只单独指定 `--output` 以免覆盖）。
    - 验证：运行后应在 `reports/` 下看到对应 `strategy_*.md` 文件，且文件内容包含“回本概率（10/20/60d）”与“风险对冲位”小节。

安全与权限
- 默认不在生产账户下执行会造成真实交易的脚本。若需要下单或调整实盘持仓，必须先得到用户口头/书面授权并记录操作理由。

变更记录与维护
- 本文件为 AI 可执行手册，任何变更应在 `agent_rules.md` 中记录变更理由与测试步骤。

最后更新：2026-05-13