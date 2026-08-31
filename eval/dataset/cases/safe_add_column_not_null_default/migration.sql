SET lock_timeout = '3s';
ALTER TABLE orders ADD COLUMN currency varchar(3) NOT NULL DEFAULT 'USD';
