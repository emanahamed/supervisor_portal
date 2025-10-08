"""One-off migration: drop legacy Book columns (min_subjects, max_subjects, image_url).

SQLite lacks DROP COLUMN prior to v3.35; approach:
 1. Inspect existing 'book' table.
 2. Create a new temp table without legacy columns.
 3. Copy data across (excluding dropped columns).
 4. Drop original table and rename temp table.

Idempotent: if legacy columns already absent, exits cleanly.
Run manually: `python migrate_drop_legacy_book_columns.py`
"""
from sqlalchemy import text

from app import app, db

LEGACY_COLS = {"min_subjects", "max_subjects", "image_url"}

with app.app_context():
    conn = db.engine.connect()
    cols = list(conn.execute(text("PRAGMA table_info(book)")))
    existing_cols = {c[1] for c in cols}
    if not LEGACY_COLS & existing_cols:
        print("[INFO] Legacy book columns already removed; nothing to do.")
    else:
        print(f"[INFO] Dropping legacy columns: {', '.join(sorted(LEGACY_COLS & existing_cols))}")
        # Build column definitions for new table
        col_defs = []
        copy_cols = []
        for cid, name, ctype, notnull, dflt, pk in cols:
            if name in LEGACY_COLS:
                continue
            copy_cols.append(name)
            # Reconstruct simple column definitions (assumes basic types/no complex constraints)
            col_def = f"{name} {ctype or ''}".strip()
            if pk:
                col_def += " PRIMARY KEY"
            if notnull:
                col_def += " NOT NULL"
            if dflt is not None:
                col_def += f" DEFAULT {dflt}"
            col_defs.append(col_def)
        tmp_table_sql = f"CREATE TABLE book_new (" + ", ".join(col_defs) + ")"
        trans = conn.begin()
        try:
            conn.execute(text(tmp_table_sql))
            conn.execute(text(f"INSERT INTO book_new ({', '.join(copy_cols)}) SELECT {', '.join(copy_cols)} FROM book"))
            conn.execute(text("DROP TABLE book"))
            conn.execute(text("ALTER TABLE book_new RENAME TO book"))
            trans.commit()
            print("[INFO] Legacy columns dropped successfully.")
        except Exception as exc:
            trans.rollback()
            print(f"[ERROR] Migration failed: {exc}")
