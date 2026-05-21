"""从 watchlist.csv 读取所有 ticker，为每个 ticker 生成策略报告到 reports/ 目录"""
import subprocess
import pathlib
import sys

# 将上级目录加入 sys.path
root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

# 读取 watchlist.csv
watchlist_path = root / "data" / "watchlist.csv"
with open(watchlist_path, "r") as f:
    line = f.read().strip()
    tickers = [t.strip() for t in line.split(",") if t.strip()]

print(f"Watchlist 中共 {len(tickers)} 个 ticker: {', '.join(tickers)}\n")

# 确保 reports 目录存在
reports_dir = root / "reports"
reports_dir.mkdir(parents=True, exist_ok=True)

# 依次运行 strategy_advisor.py
script = root / "scripts" / "strategy_advisor.py"
success = 0
failed = 0

for ticker in tickers:
    target_dir = reports_dir / ticker
    target_dir.mkdir(parents=True, exist_ok=True)
    print(f"▶️  正在生成 {ticker} 的报告...")
    result = subprocess.run(
        [sys.executable, str(script), "--ticker", ticker, "--output", str(target_dir / f"strategy_{ticker}.md")],
        capture_output=True, text=True, cwd=root
    )
    if result.returncode == 0:
        print(f"   ✅ {ticker} 报告生成成功")
        success += 1
    else:
        print(f"   ❌ {ticker} 报告生成失败:\n{result.stderr}")
        failed += 1

print(f"\n{'='*50}")
print(f"完成！成功: {success}, 失败: {failed}")