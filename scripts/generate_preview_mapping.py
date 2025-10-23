import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

import pandas as pd

from app import _shift_day_for, app
from attendance_utils import combine_all_sheets
from models import Staff, db

ctx = app.app_context()
ctx.push()

p = Path(repo_root) / 'instance' / 'imports' / 'cb83b5ae-a82e-45c5-9639-b72d47391a53.xlsx'
# Read with header detected earlier
raw = pd.read_excel(p, header=2)

df = raw
colmap = {c.lower().strip(): c for c in df.columns}

def col_any(keys):
    for k in keys:
        if k in colmap:
            return colmap[k]
    return None

machine_col = col_any(['machineid','machine_id','machine','id'])
staffid_col = col_any(['staffid','staff_id','id'])
date_col = col_any(['date','day'])
checkin_col = col_any(['checkin','check_in','timein','on','first time zone'])
checkout_col = col_any(['checkout','check_out','timeout','off','unnamed: 5','unnamed: 7'])
late_col = col_any(['late','late_minutes','late_min','late time(min)'])

print('columns detected:', df.columns.tolist())
print('machine_col:', machine_col, 'staffid_col:', staffid_col, 'date_col:', date_col, 'checkin_col:', checkin_col, 'checkout_col:', checkout_col, 'late_col:', late_col)

preview=[]
mapped_count=0
for idx,row in df.iterrows():
    if len(preview)>=100:
        break
    try:
        machine = str(row[machine_col]).strip() if machine_col and pd.notna(row.get(machine_col)) else None
        staffid = int(row[staffid_col]) if staffid_col and pd.notna(row.get(staffid_col)) else None
        d_raw = row[date_col] if date_col and pd.notna(row.get(date_col)) else None
        try:
            d = pd.to_datetime(d_raw).date() if d_raw is not None else None
        except Exception:
            d=None
        ci=None
        co=None
        try:
            if pd.notna(row.get(checkin_col)):
                ci = pd.to_datetime(row.get(checkin_col)).time()
        except Exception:
            ci=None
        try:
            if pd.notna(row.get(checkout_col)):
                co = pd.to_datetime(row.get(checkout_col)).time()
        except Exception:
            co=None
        mapped=None
        if machine:
            mapped = Staff.query.filter((Staff.whitechapel_machine_id==machine)|(Staff.east_ham_machine_id==machine)|(Staff.stratford_machine_id==machine)|(Staff.docklands_machine_id==machine)).first()
        if not mapped and staffid:
            mapped = Staff.query.filter((Staff.id==staffid)|(Staff.access_code==str(staffid))).first()
        if mapped:
            mapped_count+=1
        preview.append({'machine':machine,'staffid':staffid,'staff_name':mapped.name if mapped else None,'date':d,'check_in':str(ci) if ci else None,'check_out':str(co) if co else None})
    except Exception as e:
        continue

print('preview sample rows:', preview[:10])
print('mapped_count (first 100 rows):', mapped_count)
