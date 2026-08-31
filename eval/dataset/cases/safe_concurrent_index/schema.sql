CREATE TABLE events (
    id          bigserial PRIMARY KEY,
    session_id  uuid NOT NULL,
    kind        text NOT NULL,
    payload     jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_occurred_at ON events(occurred_at);
