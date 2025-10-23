#!/usr/bin/env python3
"""
Safe utility to add missing columns to the `staff` table in a SQLite database
without dropping or modifying existing data.

Usage:
  python3 scripts/ensure_staff_columns.py --db /absolute/or/relative/path/to/observations.db

The script will:
 - create a timestamped backup copy of the database (same dir, .bak-TIMESTAMP)
 - inspect PRAGMA table_info('staff') to find existing columns
 - run ALTER TABLE ADD COLUMN for any of the expected columns that are missing
 - report what it changed

This is intentionaly conservative: new columns are added as nullable with a
simple SQL type (TEXT/INTEGER/REAL). No data is deleted.

Make sure the Flask app is stopped before running this script.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

EXPECTED_COLUMNS = {
    # textual fields
    'first_name': 'TEXT',
    'last_name': 'TEXT',
    'dob': 'TEXT',
    'gender': 'TEXT',
    'relationship_status': 'TEXT',
    'national_insurance': 'TEXT',
    'photo': 'TEXT',
    'salary_notes': 'TEXT',
    'employment_type': 'TEXT',
    'joining_date': 'TEXT',
    'medical_condition': 'TEXT',
    'medical_condition_other': 'TEXT',
    'address_line1': 'TEXT',
    'address_line2': 'TEXT',
    'town': 'TEXT',
    'region': 'TEXT',
    'country': 'TEXT',
    'postcode': 'TEXT',
    'emergency_first_name': 'TEXT',
    'emergency_last_name': 'TEXT',
    'emergency_mobile': 'TEXT',
    'emergency_email': 'TEXT',
    'emergency_relation': 'TEXT',
    'bank_name_on_account': 'TEXT',
    'bank_name': 'TEXT',
    'bank_sort_code': 'TEXT',
    'bank_account_number': 'TEXT',
    'dbs_number': 'TEXT',
    'dbs_start_date': 'TEXT',
    'dbs_expiry_date': 'TEXT',
    'department': 'TEXT',
    'email': 'TEXT',
    'phone': 'TEXT',
    'branch': 'TEXT',
    'whitechapel_machine_id': 'TEXT',
    'east_ham_machine_id': 'TEXT',
    'stratford_machine_id': 'TEXT',
    'docklands_machine_id': 'TEXT',
    'access_code': 'TEXT',
    'name': 'TEXT',
    # numeric / id fields
    'salary_per_hour': 'REAL',
    'address_lookup_id': 'INTEGER',
    'dbs_checked_by_id': 'INTEGER',
    'company_id': 'INTEGER',
    'user_id': 'INTEGER',
    # flags / booleans
    'active': 'INTEGER',
    # timestamps
    'created_at': 'TEXT',
    'updated_at': 'TEXT',
}


def backup_db(db_path: str) -> str:
    dirname = os.path.dirname(db_path) or '.'
    base = os.path.basename(db_path)
    stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    bak_name = f"{base}.bak-{stamp}"
    bak_path = os.path.join(dirname, bak_name)
    print(f"Creating backup {bak_path} ...")
    shutil.copy2(db_path, bak_path)
    return bak_path


def get_existing_columns(conn: sqlite3.Connection) -> set:
    cur = conn.execute("PRAGMA table_info('staff')")
    rows = cur.fetchall()
    return {row[1] for row in rows}  # second column is name


def add_column(conn: sqlite3.Connection, col: str, col_type: str):
    sql = f'ALTER TABLE staff ADD COLUMN "{col}" {col_type}'
    print(f"Executing: {sql}")
    conn.execute(sql)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', '-d', default='observations.db', help='Path to sqlite database file')
    args = parser.parse_args()
    db_path = args.db
    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        sys.exit(2)

    # Ensure app is not running (best-effort check)
    # We won't forcibly kill anything; just warn.
    print("WARNING: Make sure the Flask app is stopped before running this script.")

    try:
        bak = backup_db(db_path)
    except Exception as exc:
        print(f"Failed to backup database: {exc}")
        sys.exit(3)

    conn = sqlite3.connect(db_path)
    try:
        existing = get_existing_columns(conn)
        missing = [ (c, t) for c, t in EXPECTED_COLUMNS.items() if c not in existing ]
        if not missing:
            print("No missing columns detected. Nothing to do.")
            return
        print(f"Missing columns: {[c for c, _ in missing]}\nAdding them now...")
        for col, col_type in missing:
            try:
                add_column(conn, col, col_type)
            except sqlite3.OperationalError as oe:
                print(f"Failed to add column {col}: {oe}")
        conn.commit()
        print("Done. Database has been updated. Please start your app and verify.")
        print(f"Backup created at: {bak}")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
