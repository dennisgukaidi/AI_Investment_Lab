"""Ingest all analysis metrics into the DB and display TSLA data.

This script is used to satisfy the request: load every `*_metrics.json`
under ``data/analysis`` into the SQLite database using the helper module
``_db_ingest_helper.py`` and then print the quantitative, fundamentals and
macro records for the ticker ``TSLA``.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import importlib.util

BASE = pathlib.Path(r'd:/Program/AI_Investment_Lab')

# Load the ingest helper
helper_path = BASE / 'scripts' / '_db_ingest_helper.py'
spec = importlib.util.spec_from_file_location('_db_ingest_helper', helper_path)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

# Ingest every metrics file
analysis_dir = BASE / 'data' / 'analysis'
for file in analysis_dir.iterdir():
    if file.suffix == '.json' and file.name.endswith('_metrics.json'):
        ticker = file.stem.split('_')[0]
        helper.ingest_all(ticker, file)

# Query TSLA data
conn = sqlite3.connect(BASE / 'investment_lab.db')
cur = conn.cursor()
cur.execute('SELECT date, metrics FROM quantitative WHERE ticker=?', ('TSLA',))
quant = cur.fetchall()
cur.execute('SELECT date, data FROM fundamentals WHERE ticker=?', ('TSLA',))
fund = cur.fetchall()
cur.execute('SELECT date, data FROM macro')
macro = cur.fetchall()

print('Quantitative TSLA:', json.dumps(quant, ensure_ascii=False, indent=2))
print('Fundamentals TSLA:', json.dumps(fund, ensure_ascii=False, indent=2))
print('Macro:', json.dumps(macro, ensure_ascii=False, indent=2))