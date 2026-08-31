BEGIN;
CREATE INDEX CONCURRENTLY idx_events_kind ON events(kind);
COMMIT;
