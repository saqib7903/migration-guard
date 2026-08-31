CREATE TABLE refunds (
    id serial PRIMARY KEY,
    order_id integer NOT NULL REFERENCES orders(id),
    amount_cents integer NOT NULL DEFAULT 0
);
CREATE INDEX idx_refunds_order_id ON refunds(order_id);
