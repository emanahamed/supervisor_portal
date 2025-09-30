from calendar import monthrange
from datetime import datetime
from io import BytesIO
from typing import List

import pandas as pd

# -------------------- Core Data Extraction & Cleanup Helpers -------------------- #

def parse_sheet_name(sheet_name: str) -> List[str]:
    """Given a sheet name like '1.2.3', return a list of staff IDs (strings)."""
    return [s.strip() for s in str(sheet_name).split('.') if s and s.strip()]

def convert_date(date_str: str, year: int, month: int) -> str:
    """Convert a date string in the format '01 SAT' to ISO 'YYYY-MM-DD'."""
    parts = str(date_str).split()
    if not parts:
        raise ValueError(f"Date string '{date_str}' is empty or invalid")
    try:
        day = int(parts[0])
    except Exception as e:
        raise ValueError(f"Cannot parse day from '{date_str}'") from e
    try:
        dt = datetime(year, month, day)
    except Exception as e:
        raise ValueError(f"Invalid date composed from year={year} month={month} day={day}") from e
    return dt.strftime('%Y-%m-%d')

def process_sheet(sheet_name: str, df: pd.DataFrame, year: int, month: int):
    """Process a single worksheet according to fixed column layout described.

    Staff 1: On (col 1), Off (col 3)
    Staff 2: On (col 16), Off (col 18)
    Staff 3: On (col 31), Off (col 33)
    """
    records = []
    staff_ids = parse_sheet_name(sheet_name)
    for idx, row in df.iterrows():
        date_raw = str(row.iloc[0]) if len(row) else ''
        try:
            iso_date = convert_date(date_raw, year, month)
        except Exception:
            # Skip invalid row silently (could log)
            continue
        for i, staff_id in enumerate(staff_ids[:3]):
            if i == 0:
                on_duty = row.iloc[1] if len(row) > 1 else None
                off_duty = row.iloc[3] if len(row) > 3 else None
            elif i == 1:
                on_duty = row.iloc[16] if len(row) > 16 else None
                off_duty = row.iloc[18] if len(row) > 18 else None
            else:  # i == 2
                on_duty = row.iloc[31] if len(row) > 31 else None
                off_duty = row.iloc[33] if len(row) > 33 else None
            records.append({
                'ID': staff_id,
                'Date': iso_date,
                'OnDuty': on_duty,
                'OffDuty': off_duty,
                'Name': '',
                'Department': 'Company',
                'Late time(Min)': '',
                'Leave early(Min)': '',
                'Absence(Min)': '',
                'Total(Min)': '',
                'Note': ''
            })
    return records

def combine_all_sheets(excel_file, year: int, month: int) -> pd.DataFrame:
    # Determine engine based on filename / stream name
    filename = getattr(excel_file, 'filename', None) or getattr(excel_file, 'name', '') or ''
    lower = filename.lower()
    engine = None
    if lower.endswith('.xls') and not lower.endswith('.xlsx'):
        engine = 'xlrd'
    elif lower.endswith('.xlsx'):
        engine = 'openpyxl'
    # Build ExcelFile with chosen engine (engine may be None letting pandas decide)
    xls = pd.ExcelFile(excel_file, engine=engine) if engine else pd.ExcelFile(excel_file)
    all_records = []
    for sheet_name in xls.sheet_names:
        staff_ids = parse_sheet_name(sheet_name)
        if not staff_ids or not all(s.isdigit() for s in staff_ids):
            continue
        df = pd.read_excel(excel_file, sheet_name=sheet_name, header=None, skiprows=11, engine=engine)
        all_records.extend(process_sheet(sheet_name, df, year, month))
    final_df = pd.DataFrame(all_records)
    if final_df.empty:
        return final_df
    mask_absent = (
        final_df['OnDuty'].astype(str).str.contains('ABSENT', case=False, na=False) |
        final_df['OffDuty'].astype(str).str.contains('ABSENT', case=False, na=False)
    )
    final_df = final_df[~mask_absent]
    final_df['ID'] = final_df['ID'].astype(int)
    final_df = final_df.sort_values('ID')
    final_df.rename(columns={'OnDuty': 'First time zone On-duty', 'OffDuty': 'First time zone Off-duty'}, inplace=True)
    final_df['Second time zone On-duty'] = ''
    final_df['Second time zone Off-duty'] = ''
    # Ensure time/metric columns are explicitly zero (numeric) rather than blank
    for col in ['Late time(Min)','Leave early(Min)','Absence(Min)','Total(Min)']:
        if col in final_df.columns:
            final_df[col] = 0
    cols = [
        'ID','Name','Department','Date','First time zone On-duty','First time zone Off-duty',
        'Second time zone On-duty','Second time zone Off-duty','Late time(Min)','Leave early(Min)',
        'Absence(Min)','Total(Min)','Note'
    ]
    final_df = final_df[cols]
    return final_df

def export_with_custom_header_to_bytes(final_df: pd.DataFrame, start_date_str: str, end_date_str: str) -> BytesIO:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'Exception Stat.'
    ws['A1'] = 'Exception Statistic Report'
    ws['A2'] = 'Stat.Date:'; ws['B2'] = f'{start_date_str} ~ {end_date_str}'
    ws['A3'] = 'ID'; ws['B3'] = 'Name'; ws['C3'] = 'Department'; ws['D3'] = 'Date'
    ws['E3'] = 'First time zone'; ws.merge_cells('E3:F3')
    ws['G3'] = 'Second time zone'; ws.merge_cells('G3:H3')
    ws['I3'] = 'Late time(Min)'; ws['J3'] = 'Leave early(Min)'; ws['K3'] = 'Absence(Min)'; ws['L3'] = 'Total(Min)'; ws['M3'] = 'Note'
    ws['E4'] = 'On-duty'; ws['F4'] = 'Off-duty'; ws['G4'] = 'On-duty'; ws['H4'] = 'Off-duty'
    for r_idx, row in enumerate(final_df.values, start=5):
        for c_idx, val in enumerate(row, start=1):
            ws.cell(row=r_idx, column=c_idx, value=val)
    out = BytesIO(); wb.save(out); out.seek(0); return out

def compute_date_range(year: int, month: int):
    days = monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{days}"