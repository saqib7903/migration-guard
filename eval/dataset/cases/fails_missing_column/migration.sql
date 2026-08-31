SET lock_timeout = '3s';
CREATE INDEX CONCURRENTLY idx_orders_shipped_at ON orders(shipped_at);
