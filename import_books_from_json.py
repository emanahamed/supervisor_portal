"""Utility script to populate / upsert Book records from a JSON file.

Usage:
  python import_books_from_json.py path/to/books.json [--mode upsert|create-only]

JSON Format:
  A list of objects. Supported (case-insensitive) keys / aliases:
    Book_Name / name (required)
    Price / price
    Subject / subject
    Year / year
    Cover / cover
    Cover_URL / cover_url
    Inner / inner
    Inner_URL / inner_url
    Print_Format / print_format
    Finishing / finishing
    Active / active (truthy/falsey)

Behavior:
  - By default (upsert mode), existing books matched by name are updated; new names are created.
  - In create-only mode, existing names are skipped.
  - Name comparison is case sensitive (consistent with current UI uniqueness assumption). Adjust if needed.

Exit Codes:
  0 success (even if zero changes)
  1 fatal error (file unreadable / JSON invalid)

"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

from app import app, db
from models import Book

ALIASES = {
    'name': {'book_name','name'},
    'price': {'price','Price'},
    'subject': {'subject','Subject'},
    'year_group': {'year','Year'},  # stored raw as year_group
    'cover': {'cover','Cover'},
    'cover_url': {'cover_url','Cover_URL'},
    'inner': {'inner','Inner'},
    'inner_url': {'inner_url','Inner_URL'},
    'print_format': {'print_format','Print_Format'},
    'finishing': {'finishing','Finishing'},
    'active': {'active','Active'},
}

TRUTHY = {'true','1','yes','y','on'}
FALSY = {'false','0','no','n','off',''}


def _first(data: Dict[str, Any], keys: set[str]) -> Any:
    for k in keys:
        if k in data and data[k] not in (None, ''):
            return data[k]
    return None


def coerce_active(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return True
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in TRUTHY:
        return True
    if s in FALSY:
        return False
    return True


def parse_records(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parsed = []
    for r in raw:
        name = _first(r, ALIASES['name'])
        if not name:
            continue
        rec = {
            'name': str(name).strip(),
            'price': float(_first(r, ALIASES['price']) or 0) if _first(r, ALIASES['price']) not in (None,'') else 0.0,
            'subject': (str(_first(r, ALIASES['subject'])).strip() or None) if _first(r, ALIASES['subject']) not in (None,'') else None,
            'year_group': (str(_first(r, ALIASES['year_group'])).strip() or None) if _first(r, ALIASES['year_group']) not in (None,'') else None,
            'cover': (str(_first(r, ALIASES['cover'])).strip() or None),
            'cover_url': (str(_first(r, ALIASES['cover_url'])).strip() or None),
            'inner': (str(_first(r, ALIASES['inner'])).strip() or None),
            'inner_url': (str(_first(r, ALIASES['inner_url'])).strip() or None),
            'print_format': (str(_first(r, ALIASES['print_format'])).strip() or None),
            'finishing': (str(_first(r, ALIASES['finishing'])).strip() or None),
            'active': coerce_active(_first(r, ALIASES['active'])),
        }
        parsed.append(rec)
    return parsed


def main():
    ap = argparse.ArgumentParser(description='Import / upsert books from JSON file.')
    ap.add_argument('json_path', help='Path to JSON file containing list of book objects')
    ap.add_argument('--mode', choices=['upsert','create-only'], default='upsert', help='Upsert (default) or only create new records')
    ap.add_argument('--dry-run', action='store_true', help='Parse and show summary without committing changes')
    args = ap.parse_args()

    if not os.path.isfile(args.json_path):
        print(f"[ERROR] File not found: {args.json_path}", file=sys.stderr)
        return 1

    try:
        with open(args.json_path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"[ERROR] Failed to read/parse JSON: {exc}", file=sys.stderr)
        return 1

    if isinstance(data, dict) and 'books' in data and isinstance(data['books'], list):
        data = data['books']
    if not isinstance(data, list):
        print('[ERROR] JSON root must be a list (or object with books list).', file=sys.stderr)
        return 1

    records = parse_records(data)
    if not records:
        print('[INFO] No valid book records found; nothing to do.')
        return 0

    created = 0
    updated = 0

    with app.app_context():
        for rec in records:
            existing = Book.query.filter_by(name=rec['name']).first()
            if existing:
                if args.mode == 'create-only':
                    continue
                # Update
                existing.price = rec['price']
                existing.subject = rec['subject']
                existing.year_group = rec['year_group']
                existing.cover = rec['cover']
                existing.cover_url = rec['cover_url']
                existing.inner = rec['inner']
                existing.inner_url = rec['inner_url']
                existing.print_format = rec['print_format']
                existing.finishing = rec['finishing']
                existing.active = rec['active']
                updated += 1
            else:
                b = Book(**rec)
                db.session.add(b)
                created += 1
        if args.dry_run:
            db.session.rollback()
            print(f"[DRY-RUN] Parsed: {len(records)} | Would create: {created} | Would update: {updated}")
            return 0
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"[ERROR] Commit failed: {exc}", file=sys.stderr)
            return 1

    print(f"[SUCCESS] Processed {len(records)} records. Created: {created}, Updated: {updated}")
    return 0

if __name__ == '__main__':  # pragma: no cover - script entry point
    raise SystemExit(main())
