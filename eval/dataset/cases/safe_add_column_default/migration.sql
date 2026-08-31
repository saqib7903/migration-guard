SET lock_timeout = '3s';
ALTER TABLE users ADD COLUMN locale text DEFAULT 'en';
