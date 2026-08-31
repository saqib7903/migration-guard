"""Unit tests for the deterministic half of the system (no model, no network).

Run with:  PYTHONPATH=src python3 -m pytest tests -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from migration_guard.agents import approval  # noqa: E402
from migration_guard.evidence import build_catalog, resolve  # noqa: E402
from migration_guard.llm import extract_json  # noqa: E402
from migration_guard.tools import lock_model, static_rules  # noqa: E402
from migration_guard.tools.sqlsplit import split_statements  # noqa: E402


# --------------------------------------------------------------------- splitter
def test_split_handles_dollar_quotes_and_semicolons():
    sql = """
    CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;
    ALTER TABLE t ADD COLUMN c text; -- trailing comment; not a statement
    INSERT INTO t (c) VALUES ('a;b');
    """
    stmts = split_statements(sql)
    assert len(stmts) == 3
    assert stmts[2].endswith("('a;b')")


def test_split_strips_block_comments():
    assert len(split_statements("/* drop everything; */ SELECT 1;")) == 1


# ------------------------------------------------------------------ static rules
@pytest.mark.parametrize(
    "sql,expected",
    [
        ("ALTER TABLE t ADD COLUMN c text NOT NULL;", "not_null_without_default"),
        ("CREATE INDEX i ON t(c);", "blocking_index_build"),
        ("ALTER TABLE t DROP COLUMN c;", "destructive_drop"),
        ("ALTER TABLE t ALTER COLUMN c TYPE bigint;", "unsafe_type_change"),
        ("ALTER TABLE t ADD CONSTRAINT k FOREIGN KEY (a) REFERENCES u(id);", "validating_constraint"),
        ("ALTER TABLE t RENAME COLUMN a TO b;", "renaming_in_use"),
        ("UPDATE t SET a = 1;", "long_running_backfill"),
        ("BEGIN; CREATE INDEX CONCURRENTLY i ON t(c); COMMIT;", "non_transactional_in_txn"),
    ],
)
def test_rules_fire(sql, expected):
    assert expected in static_rules.lint(sql)["candidate_risks"]


def test_no_false_alarm_on_a_brand_new_table():
    sql = "CREATE TABLE new_t (id serial PRIMARY KEY, v text); CREATE INDEX i ON new_t(v);"
    assert static_rules.lint(sql)["candidate_risks"] == []


def test_widening_varchar_is_not_reported_as_a_rewrite():
    sql = "SET lock_timeout='3s'; ALTER TABLE t ALTER COLUMN c TYPE varchar(200);"
    assert "unsafe_type_change" not in static_rules.lint(sql)["candidate_risks"]


def test_lock_timeout_demanded_only_for_strong_locks():
    assert "missing_lock_timeout" in static_rules.lint("ALTER TABLE t ADD COLUMN c text;")["candidate_risks"]
    assert "missing_lock_timeout" not in static_rules.lint(
        "SET lock_timeout='3s'; ALTER TABLE t ADD COLUMN c text;"
    )["candidate_risks"]


# -------------------------------------------------------------------- lock model
def test_lock_matrix_matches_postgres_documentation():
    assert lock_model.lock_for("CREATE INDEX i ON t(c)")["mode"] == "SHARE"
    assert lock_model.lock_for("CREATE INDEX CONCURRENTLY i ON t(c)")["mode"] == "SHARE UPDATE EXCLUSIVE"
    assert lock_model.lock_for("ALTER TABLE t ADD COLUMN c text")["mode"] == "ACCESS EXCLUSIVE"
    # SHARE blocks writes but not reads; ACCESS EXCLUSIVE blocks everything
    assert lock_model.blocked_traffic("SHARE") == {
        "SELECT": False, "SELECT FOR UPDATE": False, "INSERT/UPDATE/DELETE": True
    }
    assert all(lock_model.blocked_traffic("ACCESS EXCLUSIVE").values())


# --------------------------------------------------------------------- evidence
def test_fabricated_evidence_ids_are_discarded():
    lint = static_rules.lint("ALTER TABLE t DROP COLUMN c;")
    cat = build_catalog(lint, lock_model.analyze(["ALTER TABLE t DROP COLUMN c"]), {"available": False})
    kept = resolve(["E1", "E999", "not-an-id"], cat)
    assert len(kept) == 1 and kept[0].source == "static_lint"


# ---------------------------------------------------------------------- llm json
def test_extract_json_survives_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```\nHope that helps') == {"a": 1}
    assert extract_json('{"nested": {"b": "}"}}')["nested"]["b"] == "}"
    assert extract_json("no json here") == {}


# --------------------------------------------------------------------- approval
def test_approval_refuses_a_non_sandbox_dsn(monkeypatch):
    monkeypatch.setenv("MG_SANDBOX_DSNS", "postgresql://mg@localhost:5433/postgres")
    with pytest.raises(approval.NotASandbox):
        approval.guard("postgresql://prod@db.internal:5432/app", "Sam", "DROP TABLE t;", "c", "")


def test_approval_requires_a_named_human(monkeypatch, tmp_path):
    dsn = "postgresql://mg@localhost:5433/postgres"
    monkeypatch.setenv("MG_SANDBOX_DSNS", dsn)
    with pytest.raises(approval.ApprovalRequired):
        approval.guard(dsn, None, "ALTER TABLE t ADD COLUMN c text;", "c", "")


def test_approval_record_never_stores_credentials(tmp_path):
    p = approval.record_approval(
        "case", "ALTER TABLE t ADD COLUMN c text;", "Sam <sam@example.com>", "ok",
        "postgresql://user:secret@host:5433/db", tmp_path,
    )
    assert "secret" not in p.read_text()
