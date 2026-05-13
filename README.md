# 阿凯的 AI 美股投研系统

## 项目简介

这是一个基于人工智能的美股投资研究系统，旨在通过数据分析和智能算法辅助投资决策，实现稳健的资产增值。系统集成了 Interactive Brokers TWS API 和量化分析算法，提供从数据收集到投资报告生成的完整自动化流水线。

## 项目目标

- 🎯 跑赢通胀，实现资产保值增值
- 📊 基于数据的理性投资决策
- 🤖 利用AI技术提升研究效率
- ⏰ 健康投资，不熬夜看盘

## 核心特性

- **自动化数据流水线**：自动同步持仓、下载历史数据、收集新闻
- **量化风险分析**：蒙特卡洛模拟、IV 分位、ATR 风控
- **智能研报生成**：结合技术指标和舆情分析
- **容错降级**：TWS 优先，yfinance 备用，确保数据可用性

## 目录结构

```
AI_Investment_Lab/
├── data/
│   ├── raw/          # 原始 CSV 行情数据 ({ticker}_180d.csv)
│   ├── news/         # 新闻摘要 JSON ({ticker}_news.json)
│   ├── analysis/     # 分析结果 JSON ({ticker}_metrics.json)
│   └── watchlist.csv # 观察股票清单
├── scripts/          # Python 脚本
│   ├── portfolio_pipeline.py    # 投资组合流水线
│   ├── news_collector.py        # 新闻收集器
│   ├── analyze_report.py        # 量化分析引擎
│   └── download_watchlist_data.py # 数据下载脚本
├── reports/          # 生成的投资报告
├── rules.md          # 投资原则和持仓记录
├── agent_rules.md    # 脚本更新规则指南
├── cline_rules.md    # 开发规范
└── README.md         # 项目说明文档
```

## 快速开始

### 环境要求
- Python 3.8+
- Interactive Brokers TWS 或 IB Gateway
- 市场数据订阅（可选，用于完整功能）

### 安装步骤

1. **克隆项目**
   ```bash
   git clone <repository-url>
   cd AI_Investment_Lab
   ```

2. **创建虚拟环境**
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # Linux/Mac
   source .venv/bin/activate
   ```

3. **安装依赖**
   ```bash
   pip install ib_insync pandas numpy yfinance
   ```

4. **启动 TWS/IB Gateway**
   - 确保端口 7496 开放
   - 登录账户
   - 启用 API 连接

5. **配置观察清单**
   - 编辑 `data/watchlist.csv` 添加股票代码

## 使用指南

### 基本工作流

1. **更新数据**
   ```bash
   python scripts/portfolio_pipeline.py  # 同步持仓和下载历史数据
   python scripts/news_collector.py     # 收集最新新闻
   ```

2. **分析股票**
   ```bash
   python scripts/analyze_report.py TSLA  # 分析特定股票
   ```

3. **生成报告**
   - 基于 `data/analysis/TSLA_metrics.json` 生成研报
   - 保存至 `reports/TSLA_report.md`

### 自动化触发词（适用于 AI 助手）
- **"更新数据"**：执行数据同步
- **"分析 [Ticker]"**：生成股票分析报告
- **"自检"**：检查系统状态

## 详细运行流程

### 数据流水线
1. **持仓同步**：通过 TWS API 获取实时持仓和市值
2. **历史数据下载**：获取 500 日历日 OHLCV + IV 数据
3. **新闻收集**：获取最近 30 天相关新闻摘要
4. **数据丰富**：计算技术指标，填充分析师数据

### 分析引擎
- **概率建模**：蒙特卡洛模拟价格路径
- **波动率分析**：历史 vs 隐含波动率分位
- **风险评估**：ATR 基础的动态止损止盈
- **趋势识别**：多时间框架移动平均线分析
- **大盘对比**：相对 SPY 的超额收益分析

### 输出格式
- **Metrics JSON**：结构化量化指标
- **Markdown 报告**：人性化投资建议

## 数据下载脚本使用说明

项目提供了多个数据下载脚本：

### portfolio_pipeline.py（推荐）
- **用途**：完整投资组合流水线
- **功能**：持仓同步 + 历史数据下载 + 数据丰富
- **输出**：更新 `rules.md` 持仓表，生成 `data/raw/*.csv`

### download_watchlist_data.py
- **用途**：快速测试数据下载
- **功能**：下载观察清单中股票的 180 天数据
- **默认**：仅下载第一个股票（测试模式）

### news_collector.py
- **用途**：新闻舆情收集
- **功能**：增量更新新闻摘要
- **策略**：保留最近 50 条或 30 天内新闻

### 前置条件
* 已安装 `ib_insync`、`pandas`、`numpy`、`yfinance`
* 本机已启动并登录 TWS 或 IB Gateway，端口 `7496`，客户端 ID 为 `10`
* 账户拥有相应市场数据订阅（推荐，用于完整 IV 和分析师数据）

### 运行方式
```bash
# 完整流水线
python scripts/portfolio_pipeline.py

# 新闻更新
python scripts/news_collector.py

# 单股票分析
python scripts/analyze_report.py AAPL
```

### 输出文件格式
* **CSV 数据**：`Date,Open,High,Low,Close,Volume,IV,AnalystTargetPrice,AnalystRating`
* **新闻 JSON**：包含标题、摘要、发布时间的结构化数据
* **分析 JSON**：包含概率分布、风险指标、趋势分析的量化结果

## 常见问题

### 连接问题
- **Error 10089/10358**：检查市场数据订阅
- **连接超时**：验证 TWS 运行状态和端口配置
- **客户端冲突**：调整 clientId 参数

### 数据问题
- **缺失 IV 数据**：使用滚动 HV 填充
- **新闻获取失败**：回退至备用数据源
- **分析师数据为空**：依赖 yfinance 补充

### 性能优化
- 减少模拟次数以加快分析
- 使用增量更新避免重复下载
- 定期清理过期数据文件

## 开发指南

- **脚本修改**：参考 `cline_rules.md` 开发规范
- **问题排查**：查看 `agent_rules.md` 更新指南
- **新功能**：遵循动静分离和增量更新原则

## 技术栈

- **Python 3.x**：核心编程语言
- **ib_insync**：Interactive Brokers API 客户端
- **pandas/numpy**：数据处理和数值计算
- **yfinance**：备用数据源
- **asyncio**：异步编程支持

---

*最后更新：2026年5月13日*