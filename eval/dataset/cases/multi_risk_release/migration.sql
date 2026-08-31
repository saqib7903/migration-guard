ALTER TABLE orders ADD COLUMN fulfilled_at timestamptz NOT NULL;
CREATE INDEX idx_orders_fulfilled_at ON orders(fulfilled_at);
ALTER TABLE users DROP COLUMN is_active;
UPDATE orders SET status = 'legacy';
