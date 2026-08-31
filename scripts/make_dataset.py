#!/usr/bin/env python3
"""Regenerate eval/dataset/ from this file.

The dataset is synthetic on purpose: it ships in the repo, contains no customer data, and
every label is defensible from the PostgreSQL manual. Each case is one directory with
schema.sql, migration.sql and label.json so a reviewer can read a case without running
anything.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "dataset" / "cases"

SHOP = """CREATE TABLE users (
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
"""

EVENTS = """CREATE TABLE events (
    id          bigserial PRIMARY KEY,
    session_id  uuid NOT NULL,
    kind        text NOT NULL,
    payload     jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_occurred_at ON events(occurred_at);
"""

CASES: list[dict] = [
    # ---------------------------------------------------------------- unsafe
    dict(
        id="add_column_not_null", title="ADD COLUMN NOT NULL with no default", schema=SHOP,
        migration="ALTER TABLE orders ADD COLUMN currency varchar(3) NOT NULL;\n",
        risks=["not_null_without_default", "missing_lock_timeout", "migration_will_fail"],
        decision="block",
        notes="Fails outright on a non-empty table: PostgreSQL raises NotNullViolation.",
    ),
    dict(
        id="set_not_null_scan", title="SET NOT NULL on a populated column", schema=SHOP,
        migration="ALTER TABLE users ALTER COLUMN full_name SET NOT NULL;\n",
        risks=["not_null_without_default", "missing_lock_timeout"], decision="block",
        notes="Full scan under ACCESS EXCLUSIVE, and it fails while NULLs exist.",
    ),
    dict(
        id="blocking_index", title="CREATE INDEX without CONCURRENTLY", schema=SHOP,
        migration="CREATE INDEX idx_orders_status ON orders(status);\n",
        risks=["blocking_index_build"], decision="needs_changes",
        notes="SHARE lock blocks every INSERT/UPDATE/DELETE for the whole build.",
    ),
    dict(
        id="drop_column", title="DROP COLUMN still referenced by deployed code", schema=SHOP,
        migration="ALTER TABLE users DROP COLUMN full_name;\n",
        risks=["destructive_drop", "missing_lock_timeout"], decision="block",
        notes="Irreversible data loss plus instant breakage of running application code.",
    ),
    dict(
        id="type_change_rewrite", title="ALTER COLUMN TYPE forcing a rewrite", schema=SHOP,
        migration="ALTER TABLE orders ALTER COLUMN total_cents TYPE bigint;\n",
        risks=["unsafe_type_change", "table_rewrite", "missing_lock_timeout"], decision="block",
        notes="int -> bigint is not binary coercible: whole-table rewrite under ACCESS EXCLUSIVE.",
    ),
    dict(
        id="validating_fk", title="ADD FOREIGN KEY without NOT VALID", schema=SHOP,
        migration="ALTER TABLE order_items ADD CONSTRAINT fk_items_order "
                  "FOREIGN KEY (order_id) REFERENCES orders(id);\n",
        risks=["validating_constraint", "missing_lock_timeout"], decision="needs_changes",
        notes="Scans and locks both tables; the NOT VALID + VALIDATE split avoids it.",
    ),
    dict(
        id="rename_column", title="RENAME a column that is in use", schema=SHOP,
        migration="ALTER TABLE users RENAME COLUMN full_name TO display_name;\n",
        risks=["renaming_in_use", "missing_lock_timeout"], decision="block",
        notes="No overlap window: every running process breaks the moment it commits.",
    ),
    dict(
        id="unbounded_backfill", title="Unbounded UPDATE backfill", schema=SHOP,
        migration="UPDATE orders SET status = 'pending' WHERE status IS NULL;\n"
                  "UPDATE order_items SET quantity = 1;\n",
        risks=["long_running_backfill"], decision="needs_changes",
        notes="Rewrites every row in one transaction: long locks, WAL spike, bloat.",
    ),
    dict(
        id="concurrently_in_txn", title="CREATE INDEX CONCURRENTLY inside a transaction", schema=EVENTS,
        migration="BEGIN;\nCREATE INDEX CONCURRENTLY idx_events_kind ON events(kind);\nCOMMIT;\n",
        risks=["non_transactional_in_txn"], decision="block",
        notes="PostgreSQL refuses: CONCURRENTLY cannot run inside a transaction block.",
    ),
    dict(
        id="add_unique_constraint", title="ADD UNIQUE constraint inline", schema=SHOP,
        migration="ALTER TABLE users ADD CONSTRAINT users_email_key UNIQUE (email);\n",
        risks=["blocking_index_build", "missing_lock_timeout"], decision="needs_changes",
        notes="Builds the unique index inline under ACCESS EXCLUSIVE.",
    ),
    dict(
        id="volatile_default", title="ADD COLUMN with a volatile DEFAULT", schema=SHOP,
        migration="ALTER TABLE order_items ADD COLUMN token double precision DEFAULT random();\n",
        risks=["table_rewrite", "missing_lock_timeout"], decision="needs_changes",
        notes="A volatile default defeats the PG11 fast path and rewrites the table.",
    ),
    dict(
        id="drop_table", title="DROP TABLE", schema=SHOP,
        migration="DROP TABLE order_items;\n",
        risks=["destructive_drop", "missing_lock_timeout"], decision="block",
        notes="Total data loss; also fails or cascades depending on dependencies.",
    ),
    dict(
        id="truncate_table", title="TRUNCATE in a migration", schema=EVENTS,
        migration="TRUNCATE events;\n",
        risks=["destructive_drop", "missing_lock_timeout"], decision="block",
        notes="Deletes every row under ACCESS EXCLUSIVE.",
    ),
    dict(
        id="multi_risk_release", title="Realistic release migration with four problems", schema=SHOP,
        migration=(
            "ALTER TABLE orders ADD COLUMN fulfilled_at timestamptz NOT NULL;\n"
            "CREATE INDEX idx_orders_fulfilled_at ON orders(fulfilled_at);\n"
            "ALTER TABLE users DROP COLUMN is_active;\n"
            "UPDATE orders SET status = 'legacy';\n"
        ),
        risks=["not_null_without_default", "blocking_index_build", "destructive_drop",
               "long_running_backfill", "missing_lock_timeout", "migration_will_fail"],
        decision="block",
        notes="The shape of a real end-of-sprint migration: several risks in one file.",
    ),
    dict(
        id="drop_index_blocking", title="DROP INDEX without CONCURRENTLY", schema=SHOP,
        migration="DROP INDEX idx_orders_user_id;\n",
        risks=["blocking_index_build", "missing_lock_timeout"], decision="needs_changes",
        notes="ACCESS EXCLUSIVE on the table for the duration of the drop.",
    ),
    dict(
        id="lock_timeout_missing_only", title="Otherwise-fine DDL with no lock_timeout", schema=SHOP,
        migration="ALTER TABLE users ADD COLUMN locale text;\n",
        risks=["missing_lock_timeout"], decision="needs_changes",
        notes="PG11+ fast path makes this cheap, but with no lock_timeout it can still "
              "queue behind a long query and freeze the table. The interesting failure "
              "mode for reviewers is over-reporting a rewrite here.",
    ),
    # ------------------------------------------------------------------ safe
    dict(
        id="safe_add_column_default", title="ADD COLUMN with a constant default", schema=SHOP,
        migration="SET lock_timeout = '3s';\nALTER TABLE users ADD COLUMN locale text DEFAULT 'en';\n",
        risks=[], decision="safe",
        notes="PG11+ stores the default in the catalogue: no rewrite, no scan.",
    ),
    dict(
        id="safe_concurrent_index", title="CREATE INDEX CONCURRENTLY outside a transaction", schema=EVENTS,
        migration="CREATE INDEX CONCURRENTLY idx_events_kind ON events(kind);\n",
        risks=[], decision="safe",
        notes="SHARE UPDATE EXCLUSIVE: writes keep working.",
    ),
    dict(
        id="safe_not_valid_constraint", title="NOT VALID then VALIDATE", schema=SHOP,
        migration=(
            "SET lock_timeout = '3s';\n"
            "ALTER TABLE orders ADD CONSTRAINT orders_total_nonneg CHECK (total_cents >= 0) NOT VALID;\n"
            "ALTER TABLE orders VALIDATE CONSTRAINT orders_total_nonneg;\n"
        ),
        risks=[], decision="safe",
        notes="The textbook two-step: brief lock, then a non-blocking validation scan.",
    ),
    dict(
        id="safe_create_table", title="CREATE TABLE plus its index", schema=SHOP,
        migration=(
            "CREATE TABLE refunds (\n"
            "    id serial PRIMARY KEY,\n"
            "    order_id integer NOT NULL REFERENCES orders(id),\n"
            "    amount_cents integer NOT NULL DEFAULT 0\n"
            ");\n"
            "CREATE INDEX idx_refunds_order_id ON refunds(order_id);\n"
        ),
        risks=[], decision="safe",
        notes="A brand new table has no traffic; indexing it blocks nobody.",
    ),
    dict(
        id="safe_drop_index_concurrently", title="DROP INDEX CONCURRENTLY", schema=SHOP,
        migration="DROP INDEX CONCURRENTLY idx_orders_user_id;\n",
        risks=[], decision="safe",
        notes="SHARE UPDATE EXCLUSIVE only.",
    ),
    # ------------------------------------------------- fails only when executed
    # These are the cases a static linter structurally cannot get right: the SQL is
    # well-formed and idiomatic, and it still does not run against this schema and this
    # data. They are why the reviewer has a real database instead of a rule table.
    dict(
        id="fails_missing_column", title="Index on a column that does not exist", schema=SHOP,
        migration="SET lock_timeout = '3s';\n"
                  "CREATE INDEX CONCURRENTLY idx_orders_shipped_at ON orders(shipped_at);\n",
        risks=["migration_will_fail"], decision="block",
        notes="Looks like the safe pattern. orders.shipped_at was never added, so the "
              "deploy aborts. Only executing it finds this.",
    ),
    dict(
        id="fails_typo_syntax", title="Typo that only the server catches", schema=SHOP,
        migration="ALTER TABLE users ADD COLUMN nickname text;\n"
                  "ALTER TABLE users ADD CONSTRAIN users_nickname_len CHECK (length(nickname) < 40);\n",
        risks=["migration_will_fail", "missing_lock_timeout"], decision="block",
        notes="`CONSTRAIN` instead of `CONSTRAINT`. The first statement commits, the "
              "second aborts: a half-applied migration.",
    ),
    dict(
        id="fails_validate_violating_check", title="NOT VALID then VALIDATE on data that violates it",
        schema=SHOP,
        migration="SET lock_timeout = '3s';\n"
                  "ALTER TABLE orders ADD CONSTRAINT orders_total_min "
                  "CHECK (total_cents > 100000) NOT VALID;\n"
                  "ALTER TABLE orders VALIDATE CONSTRAINT orders_total_min;\n",
        risks=["migration_will_fail"], decision="block",
        notes="Textbook-correct shape, wrong predicate. VALIDATE fails on existing rows. "
              "A linter sees the safe pattern and approves it.",
    ),
    dict(
        id="fails_unique_after_backfill", title="Unique index over values a backfill just duplicated",
        schema=SHOP,
        migration="UPDATE orders SET status = 'pending';\n"
                  "CREATE UNIQUE INDEX CONCURRENTLY idx_orders_status_uniq ON orders(status);\n",
        risks=["long_running_backfill", "migration_will_fail"], decision="block",
        notes="Each statement is defensible alone; together the second cannot succeed. "
              "Requires reasoning about the data the first statement produced.",
    ),
    dict(
        id="fails_drop_column_dependent_view", title="DROP COLUMN with a view depending on it",
        schema=SHOP + "CREATE VIEW active_users AS SELECT id, full_name FROM users WHERE is_active;\n",
        migration="ALTER TABLE users DROP COLUMN full_name;\n",
        risks=["destructive_drop", "migration_will_fail", "missing_lock_timeout"], decision="block",
        notes="PostgreSQL refuses because a view depends on the column; CASCADE would "
              "silently drop the view instead.",
    ),
    # --------------------------------------------- safe, but easy to over-report
    dict(
        id="safe_widen_varchar", title="Widening varchar(50) to varchar(200)",
        schema=SHOP.replace("full_name   text,", "full_name   varchar(50),"),
        migration="SET lock_timeout = '3s';\n"
                  "ALTER TABLE users ALTER COLUMN full_name TYPE varchar(200);\n",
        risks=[], decision="safe",
        notes="Binary coercible: PostgreSQL only updates the catalogue, relfilenode is "
              "unchanged. Reviewers routinely call this a full rewrite.",
    ),
    dict(
        id="safe_set_not_null_after_check", title="SET NOT NULL with a validated CHECK already in place",
        schema=SHOP.replace(
            "    created_at  timestamptz NOT NULL DEFAULT now()\n);",
            "    created_at  timestamptz NOT NULL DEFAULT now(),\n"
            "    CONSTRAINT users_full_name_present CHECK (full_name IS NOT NULL)\n);",
        ).replace("full_name   text,", "full_name   text NOT NULL DEFAULT 'unknown',"),
        migration="SET lock_timeout = '3s';\nALTER TABLE users ALTER COLUMN full_name SET NOT NULL;\n",
        risks=[], decision="safe",
        notes="PG12+ uses the validated CHECK to skip the full scan. The rule-based "
              "linter cannot see that and reports a scan that will not happen.",
    ),
    dict(
        id="safe_add_column_not_null_default", title="ADD COLUMN NOT NULL *with* a constant default",
        schema=SHOP,
        migration="SET lock_timeout = '3s';\n"
                  "ALTER TABLE orders ADD COLUMN currency varchar(3) NOT NULL DEFAULT 'USD';\n",
        risks=[], decision="safe",
        notes="The PG11 fast path: no rewrite, no scan. Frequently confused with the "
              "genuinely broken no-default version.",
    ),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    index = []
    for c in CASES:
        d = OUT / c["id"]
        d.mkdir(exist_ok=True)
        (d / "schema.sql").write_text(c["schema"], encoding="utf-8")
        (d / "migration.sql").write_text(c["migration"], encoding="utf-8")
        label = {
            "case_id": c["id"],
            "title": c["title"],
            "expected_risks": sorted(c["risks"]),
            "expected_decision": c["decision"],
            "notes": c["notes"],
        }
        (d / "label.json").write_text(json.dumps(label, indent=2) + "\n", encoding="utf-8")
        index.append(label)
    (OUT.parent / "labels.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    unsafe = sum(1 for c in CASES if c["decision"] != "safe")
    print(f"wrote {len(CASES)} cases to {OUT} ({unsafe} unsafe / {len(CASES) - unsafe} safe)")


if __name__ == "__main__":
    main()
