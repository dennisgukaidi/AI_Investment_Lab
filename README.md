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
│   ├── raw/              # 原始 CSV 行情数据 ({ticker}_ohlcv.csv)
│   ├── news/             # 新闻摘要 JSON（含情绪分析）({ticker}_news.json)
│   ├── fundamentals/     # 基本面估值数据 ({ticker}_fundamentals.json)
│   ├── alternative/      # 替代数据：Google Trends + 情绪 ({ticker}_alternative.json)
│   ├── macroeconomic/    # 宏观经济指标 (macro_data.json)
│   ├── analysis/         # 分析结果 JSON ({ticker}_metrics.json)
│   └── watchlist.csv     # 观察股票清单
├── scripts/              # Python 脚本
│   ├── portfolio_pipeline.py         # 投资组合流水线（持仓 + 历史数据）
│   ├── news_collector.py             # 新闻收集器（含情绪分析）
│   ├── fundamental_data_collector.py # 基本面数据收集器
│   ├── alternative_data_collector.py # 替代数据收集器（Google Trends）
│   ├── macroeconomic_data_collector.py # 宏观经济数据收集器（FRED API）
│   └── analyze_report.py             # 量化分析引擎
├── reports/              # 生成的投资报告
├── rules.md              # 投资原则和持仓记录（自动更新）
├── agent_rules.md        # 脚本更新规则指南
├── cline_rules.md        # 开发规范
└── README.md             # 项目说明文档
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
   # 核心依赖
   pip install ib_insync pandas numpy yfinance
   
   # 新闻情绪分析
   pip install textblob
   
   # 替代数据（Google Trends）
   pip install pytrends
   
   # 宏观经济数据（FRED API）
   pip install fredapi
   ```

4. **启动 TWS/IB Gateway**
   - 确保端口 7496 开放
   - 登录账户
   - 启用 API 连接

5. **获取 FRED API 密钥（可选但推荐）**
   - 访问 https://fred.stlouisfed.org/docs/api/
   - 注册账户（免费）
   - 从个人设置获取 API 密钥  e870b142d346e73b978492b02fd7f34d

6. **配置观察清单**
   - 编辑 `data/watchlist.csv` 添加股票代码

### 使用指南

### 基本工作流

1. **更新所有数据**
   ```bash
   python scripts/portfolio_pipeline.py        # 同步持仓和下载历史数据
   python scripts/news_collector.py            # 收集新闻 + 情绪分析
   
   # 批量收集基本面数据
   python scripts/fundamental_data_collector.py AAPL
   python scripts/fundamental_data_collector.py GOOG
   # ... 或使用循环处理所有股票
   
   # 批量收集替代数据
   python scripts/alternative_data_collector.py AAPL
   python scripts/alternative_data_collector.py GOOG
   # ... 或使用循环处理所有股票
   
   # 收集宏观经济数据
   python scripts/macroeconomic_data_collector.py --api-key YOUR_FRED_API_KEY
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

#### 1. 技术面数据
- **历史价格数据**：500 日历日 OHLCV（通过 TWS/yfinance）
- **技术指标**：RSI、MACD、布林带、VIX 等
- **隐含波动率 (IV)**：基于最新市场数据

#### 2. 基本面数据
- **估值比率**：动态 P/E、P/B、P/S
- **财务健康**：债务比率、流动比率、ROE/ROA
- **增长指标**：EPS 增长率、营收增长、市值信息

#### 3. 宏观经济数据（通过 FRED API）
- **利率**：美联储基金利率、10 年期国债收益率
- **通胀**：CPI、PPI 指数
- **就业**：非农就业人数、失业率
- **增长**：季度 GDP、ISM PMI

#### 4. 情绪数据
- **新闻情绪**：TextBlob 情感分析（polarity + subjectivity）
- **搜索热度**：Google Trends 数据（12 个月历史）
- **聚合情绪**：基于新闻的综合情感评分

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

同时，文档中引用的脚本路径、文件名称均与实际文件保持一致，确保新手能够顺畅地按照说明进行操作。

*最后更新：2026年5月13日*