-- Add second proof of address upload column to dbs_application table
ALTER TABLE dbs_application ADD COLUMN proof_of_address_2_path VARCHAR(500);
