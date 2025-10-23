-- SQLite / Postgres friendly ALTER TABLE script to add new columns to the `staff` table.
-- Review before running. For Postgres you may want to adjust types (e.g., numeric). Default values are NULL to avoid locking.

-- NOTE: Always back up your database before running migrations.

-- For SQLite (execute with sqlite3):
-- sqlite3 path/to/db.sqlite3 < migrations/add_staff_fields.sql

-- For Postgres (psql):
-- psql $DATABASE_URL -f migrations/add_staff_fields.sql

PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;

-- Personal
ALTER TABLE staff ADD COLUMN first_name VARCHAR(120);
ALTER TABLE staff ADD COLUMN last_name VARCHAR(120);
ALTER TABLE staff ADD COLUMN dob DATE;
ALTER TABLE staff ADD COLUMN gender VARCHAR(40);
ALTER TABLE staff ADD COLUMN relationship_status VARCHAR(80);
ALTER TABLE staff ADD COLUMN national_insurance VARCHAR(40);
ALTER TABLE staff ADD COLUMN photo VARCHAR(255);

-- Employment
ALTER TABLE staff ADD COLUMN salary_per_hour NUMERIC(10,2);
ALTER TABLE staff ADD COLUMN salary_notes TEXT;
ALTER TABLE staff ADD COLUMN employment_type VARCHAR(60);
ALTER TABLE staff ADD COLUMN joining_date DATE;

-- Medical
ALTER TABLE staff ADD COLUMN medical_condition VARCHAR(120);
ALTER TABLE staff ADD COLUMN medical_condition_other VARCHAR(255);

-- Address
ALTER TABLE staff ADD COLUMN address_line1 VARCHAR(255);
ALTER TABLE staff ADD COLUMN address_line2 VARCHAR(255);
ALTER TABLE staff ADD COLUMN town VARCHAR(120);
ALTER TABLE staff ADD COLUMN region VARCHAR(120);
ALTER TABLE staff ADD COLUMN country VARCHAR(120);
ALTER TABLE staff ADD COLUMN postcode VARCHAR(40);
ALTER TABLE staff ADD COLUMN address_lookup_id VARCHAR(255);

-- Emergency contact
ALTER TABLE staff ADD COLUMN emergency_first_name VARCHAR(120);
ALTER TABLE staff ADD COLUMN emergency_last_name VARCHAR(120);
ALTER TABLE staff ADD COLUMN emergency_mobile VARCHAR(50);
ALTER TABLE staff ADD COLUMN emergency_email VARCHAR(255);
ALTER TABLE staff ADD COLUMN emergency_relation VARCHAR(80);

-- Bank details
ALTER TABLE staff ADD COLUMN bank_name_on_account VARCHAR(255);
ALTER TABLE staff ADD COLUMN bank_name VARCHAR(255);
ALTER TABLE staff ADD COLUMN bank_sort_code VARCHAR(20);
ALTER TABLE staff ADD COLUMN bank_account_number VARCHAR(40);

-- DBS
ALTER TABLE staff ADD COLUMN dbs_number VARCHAR(120);
ALTER TABLE staff ADD COLUMN dbs_start_date DATE;
ALTER TABLE staff ADD COLUMN dbs_expiry_date DATE;
ALTER TABLE staff ADD COLUMN dbs_checked_by_id INTEGER;

COMMIT;
PRAGMA foreign_keys = ON;
