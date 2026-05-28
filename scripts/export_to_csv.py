#!/usr/bin/env python3
"""
=============================================================================
  【全指标无损解析】报告 -> CSV / Google Sheets 导出脚本 (33字段)
=============================================================================
  功能：
    1. 零漏掉解析 reports/ 下所有 strategy_[Ticker]_[YYYYMMDD].md 报告
    2. 完整提取 33 个核心量化指标（含三时区Bootstrap、风控、估值、宏观）
    3. 生成本地 CSV 表格（UTF-8 BOM，Excel可直接打开）
    4. 支持逐行追加写入 Google Sheets（需配置 SHEET_ID）

  字段列表（33列，严格按此顺序）：
    Date, Ticker, Status, Entry_Ref, RR_Ratio, Kelly_Pct,
    Close_Price, Trend_State, RSI, IV_Rank, Crowding_Index,
    Win_Prob_10d, Risk_Loss_10d, Target_Prob_10d, Target_Median_10d,
    Win_Prob_20d, Risk_Loss_20d, Target_Prob_20d, Target_Median_20d,
    Win_Prob_60d, Risk_Loss_60d, Target_Prob_60d, Target_Median_60d,
    ATR_14, Hard_Stop_Loss, Target_Aggressive, RR_Score,
    PE_Percentile, IV_Status, SPY_State, Alpha_vs_SPY, Corr_vs_SPY,
    Action

  用法：
    # 生成本地 CSV 表格
    .venv\Scripts\python data/export_full.py

    # 测试单篇报告解析
    .venv\Scripts\python data/export_full.py reports/strategy_AAPL_20260526.md

    # 追加写入 Google Sheets（需配置 SHEET_ID）
    .venv\Scripts\python data/export_full.py --to-sheets

=============================================================================
"""

import os
import re
import sys
import csv
import glob
import logging

# ============ 第三方库（可选） ============
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials

    HAS_GSPREAD = True
except ImportError:
    HAS_GSPREAD = False


# ============ 日志 ============
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============ 路径 ============
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "data")
CREDENTIALS_FILE = os.path.join(PROJECT_ROOT, "credentials.json")

# Google Sheets 配置（使用时请修改）
SHEET_ID = "YOUR_GOOGLE_SHEET_ID_HERE"
SHEET_WORKSHEET_NAME = "Sheet1"

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


# ============ 33 字段定义 ============
HEADERS = [
    "Date",
    "Ticker",
    "Status",
    "Entry_Ref",
    "RR_Ratio",
    "Kelly_Pct",
    "Close_Price",
    "Trend_State",
    "RSI",
    "IV_Rank",
    "Crowding_Index",
    # ---- 10日视角 ----
    "Win_Prob_10d",
    "Risk_Loss_10d",
    "Target_Prob_10d",
    "Target_Median_10d",
    # ---- 20日视角 ----
    "Win_Prob_20d",
    "Risk_Loss_20d",
    "Target_Prob_20d",
    "Target_Median_20d",
    # ---- 60日视角 ----
    "Win_Prob_60d",
    "Risk_Loss_60d",
    "Target_Prob_60d",
    "Target_Median_60d",
    # ---- 风控 & 估值 & 宏观 ----
    "ATR_14",
    "Hard_Stop_Loss",
    "Target_Aggressive",
    "RR_Score",
    "PE_Percentile",
    "IV_Status",
    "SPY_State",
    "Alpha_vs_SPY",
    "Corr_vs_SPY",
    "Action",
]


# ============ 配置 ============
# 如果 True，每只股票内部按日期降序（最新在上）排序；False 则升序（最老在上）
SORT_DATE_DESC = True
# 输出的聚合文件名
TICKER_CSV_NAME = "ticker_data.csv"


# ============ 清洗函数 ============


def clean_dollar(raw):
    """去除 $, 逗号, 空格, emoji 旗帜箭头 等符号，返回 float"""
    if raw is None:
        return 0.0
    cleaned = raw.strip()
    # 移除常见符号
    for ch in ["$", ",", "📈", "📉", "🔴", "🟢", "⚠️", "💡", "⚠️"]:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def clean_number(raw):
    """去除 % 和其他符号，返回 float（不除以 100，保留百分比数值如 43.9）"""
    if raw is None:
        return 0.0
    cleaned = raw.strip().replace("%", "").strip()
    # 移除 emoji 等非数字符号
    cleaned = re.sub(r"[^\d.\-+]", "", cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def strip_emoji(raw):
    """去除行首的 emoji 符号，保留文字"""
    if not raw:
        return ""
    return re.sub(r"^[^\w\s]*", "", raw).strip()


def extract_ticker_from_filename(filename):
    """从文件名提取 Ticker"""
    m = re.search(r"strategy_([A-Z]+)_\d{8}\.md$", filename)
    return m.group(1) if m else ""


# ============ 核心解析 ============


def parse_report(file_content, filename=""):
    """
    全量解析单篇 Markdown 报告，返回 33 字段 dict。
    任何一项提取失败 => 默认值 + Warning，绝不崩溃。
    """
    res = {}

    # ---------- 辅助：从内容中匹配键值 ----------
    def get_val(pattern, group=1, default=""):
        m = re.search(pattern, file_content)
        if m:
            return m.group(group).strip()
        return default

    # ======== 1. Date ========
    res["Date"] = get_val(r"\*\*报告日期\*\*[：:]\s*(\d{4}-\d{2}-\d{2})")
    if not res["Date"]:
        logger.warning("  [WARN] Date 提取失败，设为空")

    # ======== 2. Ticker ========
    m = re.search(r"#\s*📊\s*([A-Z]+)\s", file_content)
    res["Ticker"] = m.group(1) if m else extract_ticker_from_filename(filename)

    # ======== 3. Status ========
    res["Status"] = get_val(r"\*\*持仓\*\*[：:]\s*(.*?)(?:\n|$)")
    if not res["Status"]:
        res["Status"] = "未知"

    # ======== 4. Entry_Ref ========
    raw = get_val(r"\*\*Entry Ref\*\*[：:]\s*\$?([\d,.\s]+?)(?:\s*\||\n|$)")
    res["Entry_Ref"] = clean_dollar(raw)

    # ======== 5. RR_Ratio ========
    res["RR_Ratio"] = get_val(r"\*\*R/R\*\*[：:]\s*([\d:.]+)")

    # ======== 6. Kelly_Pct ========
    raw = get_val(r"\*\*Kelly\*\*[：:]\s*([\d.]+%)")
    res["Kelly_Pct"] = clean_number(raw)

    # ======== 7. Close_Price ========
    # 注意：首次出现的"当前价"在第5行，但后面还有。取首次出现的。
    raw = get_val(r"\*\*当前价\*\*[：:]\s*\$?([\d,.\s]+?)(?:\s*\||\n|$)")
    res["Close_Price"] = clean_dollar(raw)

    # ======== 8. Trend_State ========
    m = re.search(
        r"\*\*趋势状态\*\*[：:]\s*`([^`]+)`\s*[→➡]\s*(\S+)", file_content
    )
    if m:
        res["Trend_State"] = m.group(2).strip()
    else:
        m2 = re.search(r"\*\*趋势状态\*\*[：:]\s*`([^`]+)`", file_content)
        res["Trend_State"] = m2.group(1).strip() if m2 else ""

    # ======== 9. RSI ========
    raw = get_val(r"\*\*RSI\(14\)\*\*[：:]\s*([\d.]+)")
    res["RSI"] = clean_number(raw)

    # ======== 10. IV_Rank ========
    raw = get_val(r"\*\*IV Rank\*\*[：:]\s*([\d.]+%)")
    res["IV_Rank"] = clean_number(raw)

    # ======== 11. Crowding_Index ========
    raw = get_val(r"\*\*拥挤度指数\*\*[：:]\s*([\d.]+)\s*/\s*100")
    res["Crowding_Index"] = clean_number(raw)

    # ======== 12~23. Bootstrap 三时区 ========
    # 定位三个视角节，按顺序解析
    perspective_patterns = [
        (r"###\s*10\s*日视角\s*\n(.*?)(?=\n###|\Z)", "10d"),
        (r"###\s*20\s*日视角\s*\n(.*?)(?=\n###|\Z)", "20d"),
        (r"###\s*60\s*日视角\s*\n(.*?)(?=\n###|\Z)", "60d"),
    ]

    # 初始化默认
    for suffix in ["10d", "20d", "60d"]:
        res[f"Win_Prob_{suffix}"] = 0.0
        res[f"Risk_Loss_{suffix}"] = 0.0
        res[f"Target_Prob_{suffix}"] = 0.0
        res[f"Target_Median_{suffix}"] = 0.0

    for pattern, suffix in perspective_patterns:
        section = re.search(pattern, file_content, re.DOTALL)
        if not section:
            logger.warning(f"  [WARN] 未找到 {suffix} 视角节")
            continue
        text = section.group(1)
        # Win_Prob
        m = re.search(r"\*\*始终高于成本的概率\*\*[：:]\s*([\d.]+%)", text)
        if m:
            res[f"Win_Prob_{suffix}"] = clean_number(m.group(1))
        else:
            logger.warning(f"  [WARN] Win_Prob_{suffix} 提取失败")
        # Risk_Loss
        m = re.search(r"\*\*触及止损线的风险\*\*[：:]\s*([\d.]+%)", text)
        if m:
            res[f"Risk_Loss_{suffix}"] = clean_number(m.group(1))
        else:
            logger.warning(f"  [WARN] Risk_Loss_{suffix} 提取失败")
        # Target_Prob
        m = re.search(r"\*\*触及激进目标的概率\*\*[：:]\s*([\d.]+%)", text)
        if m:
            res[f"Target_Prob_{suffix}"] = clean_number(m.group(1))
        else:
            logger.warning(f"  [WARN] Target_Prob_{suffix} 提取失败")
        # Target_Median
        m = re.search(r"\*\*中位数目标\*\*[：:]\s*\$?([\d,.]+)", text)
        if m:
            res[f"Target_Median_{suffix}"] = clean_number(m.group(1))
        else:
            logger.warning(f"  [WARN] Target_Median_{suffix} 提取失败")

    # ======== 24. ATR_14 ========
    raw = get_val(r"\*\*ATR\(14\)\*\*[：:]\s*\$?([\d,.]+)")
    res["ATR_14"] = clean_dollar(raw)

    # ======== 25. Hard_Stop_Loss ========
    raw = get_val(r"\*\*硬止损线（1\.5x ATR）\*\*[：:]\s*\$?([\d,.]+)")
    res["Hard_Stop_Loss"] = clean_dollar(raw)

    # ======== 26. Target_Aggressive ========
    raw = get_val(r"\*\*激进目标（TP）\*\*[：:]\s*\$?([\d,.]+)")
    res["Target_Aggressive"] = clean_dollar(raw)

    # ======== 27. RR_Score ========
    raw = get_val(r"\*\*风险/收益评分\*\*[：:]\s*([\d.]+)\s*/\s*10")
    res["RR_Score"] = clean_number(raw)

    # ======== 28. PE_Percentile ========
    raw = get_val(r"\*\*PE 历史分位\*\*[：:]\s*([\d.]+%)")
    res["PE_Percentile"] = clean_number(raw)

    # ======== 29. IV_Status ========
    m = re.search(r"\*\*IV 状态\*\*[：:]\s*`(\w+)`", file_content)
    res["IV_Status"] = m.group(1).strip() if m else ""

    # ======== 30. SPY_State ========
    m = re.search(r"\*\*大盘状态（SPY）\*\*[：:]\s*(\S+)", file_content)
    res["SPY_State"] = m.group(1).strip() if m else ""

    # ======== 31. Alpha_vs_SPY ========
    # 格式: 📈 +0.10% 或 📉 -0.04%
    m = re.search(
        r"\*\*20日超额收益 vs SPY\*\*[：:]\s*[📈📉]?\s*([+-]?\s*[\d.]+%)",
        file_content,
    )
    if m:
        res["Alpha_vs_SPY"] = clean_number(m.group(1))
    else:
        res["Alpha_vs_SPY"] = 0.0
        logger.warning("  [WARN] Alpha_vs_SPY 提取失败")

    # ======== 32. Corr_vs_SPY ========
    m = re.search(
        r"\*\*60日收益相关性 vs SPY\*\*[：:]\s*([+-]?\s*[\d.]+)",
        file_content,
    )
    if m:
        res["Corr_vs_SPY"] = clean_number(m.group(1))
    else:
        res["Corr_vs_SPY"] = 0.0
        logger.warning("  [WARN] Corr_vs_SPY 提取失败")

    # ======== 33. Action ========
    # 从综合建议的操作建议行中提取核心词
    # 格式: **🔴 **谨慎回避**：风险大于机遇，暂不建议介入**
    # 提取 "谨慎回避" 等
    m = re.search(
        r"\*\*([🔴🟢⚪]?\s*\*{0,2}([^：\*]+)\*{0,2}[：:])",
        file_content,
    )
    if m:
        action_raw = m.group(2).strip()
        # 进一步净化可能残留的粗体标记
        action_raw = action_raw.replace("**", "").strip()
        res["Action"] = action_raw
    else:
        # fallback: 找"操作建议"段落
        section = re.search(
            r"###\s*操作建议\s*\n(.*?)(?:\n###|\Z)", file_content, re.DOTALL
        )
        if section:
            action_text = section.group(1).strip()
            # 取第一个非空行
            for line in action_text.split("\n"):
                line = line.strip().replace("**", "").strip()
                if line:
                    res["Action"] = line
                    break
            else:
                res["Action"] = ""
        else:
            res["Action"] = ""

    return res


# ============ 构建行数据 ============


def build_row(parsed):
    """按 HEADERS 顺序构建列表"""
    return [parsed.get(h, "") for h in HEADERS]


# ============ 文件扫描 ============


def find_all_reports(base_dir=None):
    """递归扫描所有 strategy_*.md"""
    if base_dir is None:
        base_dir = REPORTS_DIR
    pattern = os.path.join(base_dir, "**", "strategy_*.md")
    files = glob.glob(pattern, recursive=True)
    files = [f for f in files if os.path.isfile(f)]
    files.sort()
    return files


# ============ Google Sheets 写入 ============


def append_to_sheets(all_rows):
    """批量追加到 Google Sheets"""
    if not HAS_GSPREAD:
        logger.error("  [ERROR] 请先安装 gspread: pip install gspread oauth2client")
        return
    if SHEET_ID == "YOUR_GOOGLE_SHEET_ID_HERE":
        logger.error(
            "  [ERROR] 请先配置 SHEET_ID！\n"
            "   创建 Google Sheets 表格 -> URL 中复制 ID -> 填入脚本"
        )
        return
    if not all_rows:
        logger.warning("  [WARN] 无数据写入")
        return

    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            CREDENTIALS_FILE, SCOPES
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_WORKSHEET_NAME)

        total = len(all_rows)
        for i, row in enumerate(all_rows):
            sheet.append_row(row, value_input_option="USER_ENTERED")
            if (i + 1) % 20 == 0 or i == total - 1:
                print(f"    >> 已写入 {i + 1}/{total} 行")

        print(f"  [OK] Google Sheets 写入完成！共 {total} 行")
    except Exception as e:
        logger.error("  [ERROR] Google Sheets 写入失败: %s", e)
        raise


# ============ 主入口 ============


def main():
    # ---- 解析命令行 ----
    to_sheets = "--to-sheets" in sys.argv
    single_file = None
    for arg in sys.argv[1:]:
        if arg == "--to-sheets":
            continue
        if os.path.isfile(arg):
            single_file = arg
            break

    print("=" * 72)
    print("  【全指标无损解析】报告 -> 表格导出工具 (33字段)")
    print("=" * 72)

    # ---- 单文件测试模式 ----
    if single_file:
        fname = os.path.basename(single_file)
        print(f"\n  [测试模式] 解析: {fname}")
        try:
            with open(single_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"  [ERROR] 读取失败: {e}")
            return

        parsed = parse_report(content, filename=fname)

        print(f"\n  {'=' * 60}")
        print(f"  解析结果（{len(HEADERS)} 个字段）")
        print(f"  {'=' * 60}")
        for h in HEADERS:
            print(f"    {h:25s}= {parsed.get(h, '')}")
        print(f"  {'=' * 60}")

        # 输出 CSV 行
        row = build_row(parsed)
        print(f"\n  CSV 行数据 (可直接复制):")
        print(f"  " + ",".join(str(v) for v in row))
        return

    # ---- 批量模式 ----
    scan_dir = REPORTS_DIR
    print(f"\n  扫描目录: {scan_dir}")

    files = find_all_reports(scan_dir)
    if not files:
        print("  [WARN] 未找到任何 strategy_*.md 文件\n")
        return

    print(f"  发现 {len(files)} 个报告文件\n")

    # 全量解析（从 reports 里把最新解析到的行收集为 parsed dicts）
    parsed_rows = []  # list of parsed dicts (not built rows)
    ok = fail = 0

    for fpath in files:
        fname = os.path.basename(fpath)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"  [FAIL] 读取失败 {fname}: {e}")
            fail += 1
            continue

        parsed = parse_report(content, filename=fname)
        # 必要时填充 Date / Ticker 的默认值
        if not parsed.get("Date"):
            logger.warning("  [WARN] 文件 %s 未提取到 Date 字段，跳过", fname)
            fail += 1
            continue
        if not parsed.get("Ticker"):
            logger.warning("  [WARN] 文件 %s 未提取到 Ticker 字段，跳过", fname)
            fail += 1
            continue

        parsed_rows.append(parsed)
        ok += 1

        if ok % 20 == 0:
            print(f"  ... 已解析 {ok}/{len(files)} 个文件")

    print(f"\n  解析统计: 成功 {ok} 个, 失败 {fail} 个")
    print(f"  待合并行数: {len(parsed_rows)}")

    # ---- 读取已有的 ticker_data.csv（如果存在）并构建索引 (Ticker, Date) -> row_dict ----
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, TICKER_CSV_NAME)

    existing = []
    index = {}  # (ticker, date) -> row_dict

    if os.path.isfile(csv_path):
        with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            file_headers = reader.fieldnames or []
            # 如果现有文件的列与 HEADERS 不一致，发出警告但仍尝试兼容
            if file_headers != HEADERS:
                logger.warning("  [WARN] 现有 CSV 列头与预设 HEADERS 不完全一致，写回时将强制使用预设 HEADERS")
            for r in reader:
                # 统一键名
                row = {h: r.get(h, "") for h in HEADERS}
                existing.append(row)
                key = (row.get("Ticker", ""), row.get("Date", ""))
                index[key] = row
        print(f"  读取现有数据: {len(existing)} 行 (来自 {csv_path})")
    else:
        print(f"  未找到现有目标文件，初始化新表: {csv_path}")

    # ---- Upsert: 覆盖或插入 ----
    inserts = updates = 0
    for parsed in parsed_rows:
        key = (parsed.get("Ticker", ""), parsed.get("Date", ""))
        # 构建按 HEADERS 的字典表示
        rowdict = {h: parsed.get(h, "") for h in HEADERS}

        if key in index:
            # 覆盖旧行
            index[key].update(rowdict)
            updates += 1
            logger.info("发现重复并覆盖: Ticker: %s, Date: %s", key[0], key[1])
            print(f"  [UPDATE] 发现重复并覆盖: Ticker: {key[0]}, Date: {key[1]}")
        else:
            index[key] = rowdict
            inserts += 1
            logger.info("插入新数据: Ticker: %s, Date: %s", key[0], key[1])
            print(f"  [INSERT] 新数据: Ticker: {key[0]}, Date: {key[1]} 已插入")

    print(f"\n  合并结果: 新插入 {inserts} 行, 覆盖 {updates} 行, 总计 {len(index)} 行")

    # ---- 聚合并排序 ----
    from datetime import datetime

    # 将 index 转为按 Ticker 分组的字典
    grouped = {}
    for (ticker, date), row in index.items():
        grouped.setdefault(ticker, []).append(row)

    # 结果按 Ticker 升序，Ticker 内按 Date 排序（配置 SORT_DATE_DESC 控制降序）
    ordered_rows = []
    for ticker in sorted(grouped.keys()):
        rows = grouped[ticker]
        def parse_date_safe(d):
            try:
                return datetime.fromisoformat(d)
            except Exception:
                return datetime.min

        rows.sort(key=lambda r: parse_date_safe(r.get("Date", "")), reverse=bool(SORT_DATE_DESC))
        ordered_rows.extend(rows)

    # ---- 写回 CSV（utf-8-sig） ----
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        for r in ordered_rows:
            writer.writerow([r.get(h, "") for h in HEADERS])

    print(f"\n  [OK] 聚合并写回: {csv_path}")
    print(f"       总股票组数: {len(grouped)}, 总行数: {len(ordered_rows)}")

    # ---- Google Sheets 写入（可选，保持原逻辑） ----
    if to_sheets:
        print(f"\n  [--to-sheets] 准备写入 Google Sheets ...")
        # 将 ordered_rows 转换为 list[list] 按 HEADERS 顺序
        sheet_rows = [[r.get(h, "") for h in HEADERS] for r in ordered_rows]
        append_to_sheets(sheet_rows)

    print("=" * 72)


if __name__ == "__main__":
    main()