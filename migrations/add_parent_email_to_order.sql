-- Add parent_email field to order table for enrollment confirmation emails
-- Created: 2026-02-09

-- Add parent_email column to order table
ALTER TABLE "order" ADD COLUMN parent_email VARCHAR(255);

-- Add index for faster lookups
CREATE INDEX idx_order_parent_email ON "order"(parent_email);

-- Update existing orders to have NULL parent_email (already the default for new column)
-- No data migration needed as this is a new field
