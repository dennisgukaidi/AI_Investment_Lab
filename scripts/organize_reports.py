from pathlib import Path
import re

REPORTS_DIR = Path(__file__).resolve().parents[1] / 'reports'
PATTERN = re.compile(r'strategy_([A-Z0-9]+)_.+\.md$')

if not REPORTS_DIR.exists():
    print(f"Reports directory not found: {REPORTS_DIR}")
    raise SystemExit(1)

moved = []
skipped = []

for p in REPORTS_DIR.iterdir():
    if p.is_dir():
        continue
    name = p.name
    m = PATTERN.match(name)
    if not m:
        skipped.append(name)
        continue
    ticker = m.group(1)
    dest_dir = REPORTS_DIR / ticker
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if dest.exists():
        print(f"Destination exists, overwriting: {dest}")
    p.rename(dest)
    moved.append((name, str(dest)))

print("\nSummary:")
print(f"  Moved: {len(moved)} files")
for old, new in moved:
    print(f"    {old} -> {new}")
print(f"  Skipped (non-matching): {len(skipped)} files")
for s in skipped:
    print(f"    {s}")
