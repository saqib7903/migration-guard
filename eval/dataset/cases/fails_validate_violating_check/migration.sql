SET lock_timeout = '3s';
ALTER TABLE orders ADD CONSTRAINT orders_total_min CHECK (total_cents > 100000) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_total_min;
