import json
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
# ensure repo root is on sys.path
sys.path.insert(0, str(repo_root))

import pandas as pd

from attendance_utils import combine_all_sheets

p = Path(repo_root) / 'instance' / 'imports' / 'cb83b5ae-a82e-45c5-9639-b72d47391a53.xlsx'
print('file:', p, 'exists:', p.exists())
if not p.exists():
    print('Demo file not found:', p)
    raise SystemExit(1)

try:
    df = combine_all_sheets(str(p))
    print('Used combine_all_sheets')
except Exception as e:
    print('combine_all_sheets failed:', e)
    try:
        df = pd.read_excel(p)
        print('Used pandas.read_excel')
    except Exception as e2:
        print('pandas.read_excel failed:', e2)
        raise

print('rows:', len(df))
print('columns:', list(df.columns))

# print first 200 rows as JSON records
preview = df.head(200).fillna('').to_dict(orient='records')
print(json.dumps({'preview_count': len(preview), 'preview': preview}, default=str, indent=2))
