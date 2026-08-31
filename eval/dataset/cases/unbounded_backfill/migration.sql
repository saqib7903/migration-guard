UPDATE orders SET status = 'pending' WHERE status IS NULL;
UPDATE order_items SET quantity = 1;
