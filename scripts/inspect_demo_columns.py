import json
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

import pandas as pd

from attendance_utils import combine_all_sheets

p = Path(repo_root) / 'instance' / 'imports' / 'cb83b5ae-a82e-45c5-9639-b72d47391a53.xlsx'
print('file exists:', p.exists(), 'size:', p.stat().st_size)

try:
    df = combine_all_sheets(str(p))
    print('Used combine_all_sheets')
except Exception as e:
    print('combine_all_sheets failed:', e)
    df = pd.read_excel(p)
    print('Used pandas.read_excel')

print('\nCOLUMNS:')
for i,c in enumerate(df.columns):
    print(i, repr(c))

print('\nSAMPLE ROWS (first 20), transposed for readability:')
for idx, row in df.head(20).iterrows():
    print('\n---- ROW', idx, '----')
    for c in df.columns:
        val = row[c]
        if pd.isna(val):
            val = ''
        print(f"{c}: {val}")

# print some summary heuristics for columns that look like machine ids (numeric, short strings)
print('\nHeuristic candidates for machine id (unique values count and sample):')
for c in df.columns:
    nonnull = df[c].dropna().astype(str)
    if nonnull.empty:
        continue
    # pick columns where values are short (<=12) and many numeric/alphanumeric
    samples = nonnull.head(50).unique()[:5]
    maxlen = nonnull.map(len).max()
    alpha_numeric_ratio = (nonnull.str.isalnum().mean())
    if maxlen <= 20 and alpha_numeric_ratio > 0.5:
        print(f"{c!r}: maxlen={maxlen}, alpha_num_ratio={alpha_numeric_ratio:.2f}, unique_count={nonnull.nunique()}, samples={list(samples)}")

print('\nDone')
