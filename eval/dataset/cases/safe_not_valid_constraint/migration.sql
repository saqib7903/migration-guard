SET lock_timeout = '3s';
ALTER TABLE orders ADD CONSTRAINT orders_total_nonneg CHECK (total_cents >= 0) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_total_nonneg;
