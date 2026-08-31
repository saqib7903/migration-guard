CREATE TABLE users (
    id          serial PRIMARY KEY,
    email       text NOT NULL,
    full_name   text,
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE orders (
    id          serial PRIMARY KEY,
    user_id     integer NOT NULL REFERENCES users(id),
    status      text NOT NULL DEFAULT 'pending',
    total_cents integer NOT NULL DEFAULT 0,
    placed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE order_items (
    id        serial PRIMARY KEY,
    order_id  integer NOT NULL REFERENCES orders(id),
    sku       text NOT NULL,
    quantity  integer NOT NULL DEFAULT 1
);
CREATE INDEX idx_orders_user_id ON orders(user_id);
