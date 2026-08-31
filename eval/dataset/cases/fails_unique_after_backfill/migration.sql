UPDATE orders SET status = 'pending';
CREATE UNIQUE INDEX CONCURRENTLY idx_orders_status_uniq ON orders(status);
