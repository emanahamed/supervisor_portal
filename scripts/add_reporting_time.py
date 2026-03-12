"""Add reporting_time columns to mock_test and mock_test_booking_item tables."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from sqlalchemy import text

from app import app, db

with app.app_context():
    conn = db.engine.connect()

    cols = [r[1] for r in conn.execute(text("PRAGMA table_info(mock_test)"))]
    if 'reporting_time' not in cols:
        conn.execute(text("ALTER TABLE mock_test ADD COLUMN reporting_time VARCHAR(100)"))
        conn.commit()
        print("Added reporting_time to mock_test")
    else:
        print("mock_test.reporting_time already exists")

    cols2 = [r[1] for r in conn.execute(text("PRAGMA table_info(mock_test_booking_item)"))]
    if 'test_reporting_time' not in cols2:
        conn.execute(text("ALTER TABLE mock_test_booking_item ADD COLUMN test_reporting_time VARCHAR(100)"))
        conn.commit()
        print("Added test_reporting_time to mock_test_booking_item")
    else:
        print("mock_test_booking_item.test_reporting_time already exists")

    conn.close()
    print("Done")
