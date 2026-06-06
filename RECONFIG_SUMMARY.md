# Strategy Radar 重构总结 v2.2

**重构时间**：2026-06-05  
**目标对齐**：100% 对标核心交易协议的严苛标准

---

## 📋 四大重构模块

### 1️⃣ 宏观熔断器双重校验 ✅

**修改位置**：`MacroGatekeeper` 类 + `RadarDataRepo`

**核心逻辑**：
```python
# 旧逻辑（单点校验）
if SPY in ['bull_core', 'neutral_bull']:
    system.status = "OPERATIONAL"

# 新逻辑（双重校验）
if SPY in ['bull_core', 'neutral_bull'] AND QQQ in ['bull_core', 'neutral_bull']:
    system.status = "OPERATIONAL"
else:
    system.status = "MELTDOWN"  # 任意一个不满足即熔断
```

**具体改动**：
- ✅ 新增 `_infer_qqq_regime_from_any()` 方法
- ✅ 修改 `get_latest_macro_state()` 同时获取 SPY + QQQ 状态
- ✅ 修改 `MacroGatekeeper.evaluate()` 实现双重校验判定
- ✅ 报告中展示 SPY + QQQ 的独立状态和校验结果

**当前状态**：
- SPY: `bull_core` ✅
- QQQ: `unknown` ❌  
→ **系统状态**：MELTDOWN（严苛保护生效）

---

### 2️⃣ 三维矩阵 3 日时空共振 ✅

**修改位置**：`ReverseCoreBuyProtocol.evaluate_ticker()` 方法

**硬编码严苛标准**（替代动态百分位）：
```python
PE_PERCENTILE_THRESHOLD = 15.0    # PE分位 ≤ 15%
RSI_THRESHOLD = 33.0              # RSI ≤ 33
CROWDING_THRESHOLD = 5.0          # Crowding ≤ 5.0
```

**触发逻辑**：
1. **估值轴**：PE分位 ≤ 15%（3日窗口内任意一天）
2. **动能轴**：RSI ≤ 33 **且** Crowding ≤ 5.0（3日窗口内任意一天）
3. **概率轴**：Win_Prob_10d 从 0% 冰点首次向上扭转 > 5%（**必须今天**）

**共振触发条件**：
```
估值✅ AND 动能✅ AND 概率轴今天扭转✅ → Level-1 黄金建仓点
```

**具体改动**：
- ✅ 新增全局常量 `RSI_THRESHOLD` 和 `CROWDING_THRESHOLD`
- ✅ 移除动态百分位计算（改用硬编码严苛值）
- ✅ 使用 Pandas 滚动窗口检测 3 日触发
- ✅ 严格验证概率轴必须在当日扭转

**当前监控状态**：
```
标的   | PE分位 | RSI  | Crowding | 评估结果
-------|--------|------|----------|----------
AXP   | 5.9%   | 49.5 | 0.0      | PE✅ RSI✗ CR✓
CEG   | 11.8%  | 39.9 | 25.0     | PE✅ RSI✗ CR✗
```
→ **当前无标的同时满足三维共振**（系统已熔断，不扫描）

---

### 3️⃣ 实战执行动态风控输出 ✅

**修改位置**：报告生成器 + 模块二、三输出

**【模块二】左侧建仓风控表格**：
```
| 入场价 | $XXX.XX |
| 左侧硬止损（2.0×ATR) | $XXX.XX |
| 建议仓位 | 10%-15% 试探性底仓 |
| ATR(14) | $XX.XX |
```

**【模块三】主力风控追踪**：
对 `active_holdings` 中的已有持仓，新增：
```
【主力风控追踪】
- 移动成本价：$XXX.XX
- 主力止损线（1.5×ATR）：$XXX.XX
- 触发止损：跌破上述价格立即止损
```

**具体改动**：
- ✅ 修改 `_module2_left_entry()` 添加风控表格输出
- ✅ 修改 `_module3_right_addition()` 新增主力风控追踪板块
- ✅ 修改 `RightSideAddition.evaluate_addition()` 返回 ATR 数据
- ✅ 新增 `position_range` 字段明确仓位区间

**示例输出**（待触发信号）：
```
### AAPL 🔷 持仓追踪

#### 当前持仓信息
- 移动成本价：$300.00
- 持仓数量：100 股

#### 🛡️ 主力风控追踪（移动止损）
- 主力止损线（1.5×ATR）：$290.50
- ATR(14)：$6.33
- 触发止损：如价格跌破 $290.50，立即止损
```

---

### 4️⃣ 强牛股防洗飞协议补全 ✅

**修改位置**：`AntiWashoutProtocol` 类的 `_evaluate_path_a()` 和 `_evaluate_path_b()` 方法

#### **路径 A：动能修复-防假跌破**

**动态 alpha 计算**：
```python
if ATR(14) / Price > 4.0%:
    alpha = 0.015  # 高波动
else:
    alpha = 0.010  # 标准波动

threshold_price = Entry_Ref × (1 + alpha)
```

**触发确认条件**：
```python
# 必须连续 2 个交易日站上阈值
if price[-1] >= threshold_price AND price[-2] >= threshold_price:
    path_a_triggered = True
```

**具体改动**：
- ✅ 添加 `[高波动/标准波动]` 标识
- ✅ 动态计算 alpha（基于 ATR/Price 比率）
- ✅ 改进连续 2 日确认逻辑（使用 `values` 数组直接比较）
- ✅ 详细输出触发阈值和当前价格

#### **路径 B：指标共振修复-防低位直接 V 反**

**核心条件**：
1. **RSI 从 ≤ 33 冰点区强力向上突破 40**
   ```python
   if prev_rsi <= 33 AND curr_rsi >= 40:
       rsi_breakthrough = True
   ```

2. **大盘当天为多头（SPY + QQQ 同时满足）**
   ```python
   if spy_regime in ['bull_core', 'neutral_bull'] AND \
      qqq_regime in ['bull_core', 'neutral_bull']:
       market_bull = True
   ```

**具体改动**：
- ✅ 修改 RSI 突破检测（前日 ≤ 33 → 当日 ≥ 40）
- ✅ 改为双重大盘校验（同时检查 SPY 和 QQQ）
- ✅ 移除 RSI 连续上升的冗余判断
- ✅ 简化到仅 2 个核心条件（RSI 突破 + 大盘多头）

---

## 📊 报告示例

### 当前运行结果
```
系统状态：🔴 MELTDOWN
原因：QQQ 状态 unknown（≠ bull_core/neutral_bull）
```

### 大盘状态输出
```
## 📡 模块一：宏观熔断器与标的筛选（双重校验）

### 大盘状态（必须 SPY + QQQ 同时满足）
- SPY State：`bull_core` ✅
- QQQ State：`unknown` ❌

### 双重校验熔断逻辑
if SPY in ['bull_core', 'neutral_bull'] AND QQQ in ['bull_core', 'neutral_bull']:
    system.status = "OPERATIONAL"  ✅ 安全
else:
    system.status = "MELTDOWN"     🔴 熔断左侧开仓
→ 当前状态：**MELTDOWN**
```

---

## ✅ 验收清单

| 需求项 | 状态 | 备注 |
|--------|------|------|
| 双重校验（SPY+QQQ） | ✅ | 系统正确熔断（QQQ 未知） |
| 三维矩阵硬编码标准 | ✅ | PE≤15%, RSI≤33, Crowding≤5.0 |
| 3 日滚动窗口 | ✅ | 估值/动能任意一天触发，概率轴今日扭转 |
| 建议仓位 10%-15% | ✅ | 报告中明确显示 |
| 左侧硬止损 2.0×ATR | ✅ | 计算公式正确 |
| 主力止损 1.5×ATR | ✅ | 模块三新增追踪 |
| 动态 alpha 计算 | ✅ | 高波动 1.5%, 标准 1.0% |
| 连续 2 日确认 | ✅ | Path A 完整实现 |
| RSI 冰点突破 40 | ✅ | Path B 改为严格的前日条件 |
| 双重大盘校验 | ✅ | Path B 同时校验 SPY+QQQ |

---

## 🚀 后续调试建议

1. **QQQ 状态缺失**  
   - 检查 quantitative 表中 QQQ 的 market_context 是否正确生成
   - 确保上游数据管道输出了 `qqq_trend_regime` 字段

2. **触发信号验证**  
   - 待大盘 QQQ 状态转为 `bull_core`/`neutral_bull` 后，系统将解除熔断
   - 监控是否有标的同时满足三维共振条件

3. **防洗飞回补测试**  
   - 需要 holdings.csv 中有近 5 天内的止损卖出记录
   - 当满足 Path A/B 条件时触发回补信号

---

**脚本版本**：v2.2  
**对齐标准**：100% 核心交易协议  
**最后更新**：2026-06-05 08:33 UTC
