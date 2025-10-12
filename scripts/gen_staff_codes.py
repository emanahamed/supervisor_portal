#!/usr/bin/env python3
"""
Generate 6-digit unique access codes for Staff records.

Usage:
  python -m scripts.gen_staff_codes            # backfill missing codes only
  python -m scripts.gen_staff_codes --force    # regenerate codes for all staff
"""
from __future__ import annotations

import argparse
import random

from app import app, db
from models import Staff


def gen_code() -> str:
    return f"{random.randint(0, 999999):06d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate 6-digit access codes for staff")
    parser.add_argument("--force", action="store_true", help="Regenerate for all staff (overwrite existing)")
    args = parser.parse_args()

    with app.app_context():
        q = Staff.query
        if not args.force:
            q = q.filter((Staff.access_code.is_(None)) | (Staff.access_code == ""))
        staff_list = q.all()
        if not staff_list:
            print("No staff require updates.")
            return 0
        existing = set()
        if not args.force:
            existing = {c for (c,) in db.session.query(Staff.access_code).filter(Staff.access_code.isnot(None)).all()}
        updated = 0
        for s in staff_list:
            if args.force:
                s.access_code = None
            code = gen_code()
            tries = 0
            while code in existing and tries < 20:
                code = gen_code()
                tries += 1
            s.access_code = code
            existing.add(code)
            updated += 1
        db.session.commit()
        print(f"Updated access codes for {updated} staff member(s).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
