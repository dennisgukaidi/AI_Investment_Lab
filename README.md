# 阿凯的 AI 美股投研系统

## 项目简介

这是一个基于人工智能的美股投资研究系统，旨在通过数据分析和智能算法辅助投资决策，实现稳健的资产增值。

## 项目目标

- 🎯 跑赢通胀，实现资产保值增值
- 📊 基于数据的理性投资决策
- 🤖 利用AI技术提升研究效率
- ⏰ 健康投资，不熬夜看盘

## 目录结构

```
AI_Investment_Lab/
├── data/
│   └── raw/          # 存放下载的原始 CSV 行情数据
├── scripts/          # 存放 Python 脚本
├── reports/          # 存放生成的周报和分析结果
├── README.md         # 项目说明文档
├── rules.md          # 投资原则和持仓记录
└── .gitignore        # Git 忽略文件配置
```

## 使用说明

1. 将下载的股票数据存放在 `data/raw/` 目录
2. Python 分析脚本放置在 `scripts/` 目录
3. 生成的分析报告和周报保存在 `reports/` 目录

## 数据下载脚本使用说明

项目提供了 `scripts/download_watchlist_data.py`，用于按照 **cline_rules.md** 中的规范，从 Interactive Brokers TWS 下载 watchlist 中股票的 180 天日线行情、隐含波动率 (IV) 以及分析师目标价。

### 前置条件
* 已安装 `ib_insync`、`pandas`（脚本会在运行前检查依赖）。
* 本机已启动并登录 TWS 或 IB Gateway，端口 `7496`，客户端 ID 为 `10`（可在脚本中修改）。
* 账户必须拥有相应的 **市场数据订阅**，包括隐含波动率（generic tick `100`）和基础面数据（`ReportSnapshot`），否则会收到 `Error 10089`、`Error 10358` 并导致相应字段为空。

### 运行方式
```bash
python scripts/download_watchlist_data.py
```
脚本会读取 `data/watchlist.csv`，默认仅下载列表中的第一只股票作为快速测试。若需要批量下载，只需将 `test_symbol = symbols[0]` 替换为遍历 `symbols` 的循环即可。

### 输出
* CSV 文件保存在 `data/raw/{ticker}_180d.csv`，列结构为 `Date,Open,High,Low,Close,Volume,IV,AnalystTargetPrice,AnalystRating`。
* 下载完成后，脚本会尝试获取实时最新价并与 CSV 最后一行的收盘价进行对比，以验证数据一致性。

### 常见问题
* **Error 10089 / 10358**：说明当前账户未订阅所需的市场数据或基础面数据，请联系 IB 客服开通相应订阅。
* **实时价与 CSV 收盘价不一致**：可能是因为行情已更新，建议重新运行脚本或检查网络延迟。

## 技术栈

- Python 3.x
- 数据处理与分析库
- AI/ML 相关框架

---

*最后更新：2026年5月*