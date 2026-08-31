"""Deterministic static analysis of a migration file.

These rules never hallucinate: each one points at a literal statement. They are
intentionally *high recall, moderate precision* -- they generate candidates, and the
shadow database plus the verifier agent decide what survives. Running the linter alone
is also the ablation `--system rules_only` used in the improvement changelog.
"""
from __future__ import annotations

import re
from typing import Any

from .sqlsplit import split_statements

# type pairs postgres can change without rewriting the table
_BINARY_COERCIBLE = {
    ("varchar", "text"), ("text", "varchar"), ("varchar", "varchar"),
    ("cidr", "inet"), ("xml", "text"), ("numeric", "numeric"),
}
_VOLATILE_DEFAULT = re.compile(r"DEFAULT\s+(random\s*\(|clock_timestamp\s*\(|nextval\s*\()", re.I)


def _find(pat: str, s: str) -> bool:
    return re.search(pat, s, re.I | re.S) is not None


def _table_of(stmt: str) -> str:
    m = re.search(r"(?:ALTER|DROP|CREATE)\s+TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([\w.\"]+)", stmt, re.I)
    if m:
        return m.group(1).strip('"')
    m = re.search(r"\bON\s+([\w.\"]+)", stmt, re.I)
    if m:
        return m.group(1).strip('"')
    m = re.search(r"^\s*(?:UPDATE|DELETE\s+FROM|INSERT\s+INTO)\s+([\w.\"]+)", stmt, re.I)
    if m:
        return m.group(1).strip('"')
    return ""


def _type_of(stmt: str) -> tuple[str, str]:
    m = re.search(r"ALTER\s+COLUMN\s+[\w\"]+\s+(?:SET\s+DATA\s+)?TYPE\s+(\w+)", stmt, re.I)
    return ("", m.group(1).lower() if m else "")


def lint(migration_sql: str) -> dict[str, Any]:
    stmts = split_statements(migration_sql)
    upper_all = migration_sql.upper()
    in_explicit_txn = bool(re.search(r"^\s*BEGIN\b|^\s*START\s+TRANSACTION\b", migration_sql, re.I | re.M))
    has_lock_timeout = "LOCK_TIMEOUT" in upper_all
    findings: list[dict[str, Any]] = []

    def add(risk: str, severity: str, stmt: str, rationale: str) -> None:
        findings.append(
            {
                "risk": risk,
                "severity": severity,
                "statement": re.sub(r"\s+", " ", stmt).strip()[:240],
                "rationale": rationale,
                "table": _table_of(stmt),
                "rule_id": f"MG-{risk}",
            }
        )

    takes_strong_lock = False
    # tables created by this very migration carry no production traffic, so index builds
    # and locks on them are harmless. Tracking this removes a whole class of false alarms.
    created_here: set[str] = {
        m.group(1).strip('"').lower()
        for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", migration_sql, re.I)
    }

    for s in stmts:
        S = re.sub(r"\s+", " ", s).strip()
        target = _table_of(S).lower()
        if target and target in created_here and not _find(r"^\s*DROP\b", S):
            continue  # brand-new relation: nothing is reading it yet
        # any statement taking ACCESS EXCLUSIVE justifies demanding a lock_timeout
        if _find(r"^\s*(ALTER\s+TABLE|DROP\s+TABLE|TRUNCATE)\b", S) or (
            _find(r"^\s*DROP\s+INDEX\b", S) and not _find(r"CONCURRENTLY", S)
        ):
            takes_strong_lock = True

        if _find(r"^\s*(BEGIN|COMMIT|START TRANSACTION|SET|--)", S):
            if _find(r"CONCURRENTLY", S):
                pass
            else:
                continue

        # --- destructive ---------------------------------------------------
        if _find(r"ALTER\s+TABLE\b.*\bDROP\s+COLUMN\b", S):
            add("destructive_drop", "critical", S,
                "DROP COLUMN destroys data and breaks any deployed code still selecting that column.")
            takes_strong_lock = True
        if _find(r"^\s*DROP\s+TABLE\b", S):
            add("destructive_drop", "critical", S, "DROP TABLE destroys the relation and all data in it.")
            takes_strong_lock = True
        if _find(r"^\s*TRUNCATE\b", S):
            add("destructive_drop", "critical", S, "TRUNCATE removes every row under ACCESS EXCLUSIVE.")
            takes_strong_lock = True

        # --- NOT NULL ------------------------------------------------------
        if _find(r"ADD\s+COLUMN\b", S) and _find(r"\bNOT\s+NULL\b", S) and not _find(r"\bDEFAULT\b", S):
            add("not_null_without_default", "high", S,
                "ADD COLUMN NOT NULL without DEFAULT fails outright on a non-empty table.")
            takes_strong_lock = True
        if _find(r"ALTER\s+COLUMN\s+[\w\"]+\s+SET\s+NOT\s+NULL", S):
            add("not_null_without_default", "high", S,
                "SET NOT NULL performs a full table scan while holding ACCESS EXCLUSIVE. "
                "Safe pattern: add a NOT VALID CHECK constraint, VALIDATE it, then SET NOT NULL.")
            takes_strong_lock = True

        # --- rewrites ------------------------------------------------------
        _, newtype = _type_of(S)
        if newtype:
            oldish = "varchar" if "VARCHAR" in S.upper() else ""
            if (oldish, newtype) not in _BINARY_COERCIBLE:
                add("unsafe_type_change", "critical", S,
                    f"ALTER COLUMN TYPE -> {newtype} is generally not binary-coercible, so PostgreSQL "
                    "rewrites the entire table under ACCESS EXCLUSIVE.")
                add("table_rewrite", "critical", S, "Full table rewrite: duration scales with table size.")
            takes_strong_lock = True
        if _find(r"ADD\s+COLUMN\b", S) and _VOLATILE_DEFAULT.search(S):
            add("table_rewrite", "high", S,
                "ADD COLUMN with a volatile DEFAULT cannot use the PG11 fast path and rewrites the table.")
            takes_strong_lock = True
        if _find(r"^\s*(CLUSTER|VACUUM\s+FULL)\b", S):
            add("table_rewrite", "critical", S, "CLUSTER / VACUUM FULL rewrite the table under ACCESS EXCLUSIVE.")
            takes_strong_lock = True

        # --- indexes -------------------------------------------------------
        if _find(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\b", S) and not _find(r"CONCURRENTLY", S):
            add("blocking_index_build", "high", S,
                "CREATE INDEX without CONCURRENTLY holds SHARE, blocking all writes for the whole build.")
        if _find(r"^\s*DROP\s+INDEX\b", S) and not _find(r"CONCURRENTLY", S):
            add("blocking_index_build", "medium", S,
                "DROP INDEX without CONCURRENTLY takes ACCESS EXCLUSIVE on the table.")
            takes_strong_lock = True
        if _find(r"ADD\s+(CONSTRAINT\s+[\w\"]+\s+)?(PRIMARY\s+KEY|UNIQUE)\b", S) and not _find(r"USING\s+INDEX", S):
            add("blocking_index_build", "high", S,
                "ADD PRIMARY KEY / UNIQUE builds the index inline under ACCESS EXCLUSIVE. "
                "Safe pattern: CREATE UNIQUE INDEX CONCURRENTLY, then ADD CONSTRAINT ... USING INDEX.")
            takes_strong_lock = True

        # --- constraints ---------------------------------------------------
        if _find(r"ADD\s+CONSTRAINT\b.*\b(FOREIGN\s+KEY|CHECK)\b", S) and not _find(r"NOT\s+VALID", S):
            add("validating_constraint", "high", S,
                "ADD CONSTRAINT validates existing rows while holding a strong lock. "
                "Safe pattern: ADD CONSTRAINT ... NOT VALID, then VALIDATE CONSTRAINT.")
            takes_strong_lock = True

        # --- renames -------------------------------------------------------
        if _find(r"ALTER\s+TABLE\b.*\bRENAME\b", S):
            add("renaming_in_use", "high", S,
                "RENAME breaks currently deployed code instantly; there is no overlap window. "
                "Safe pattern: add the new name, dual-write, migrate readers, drop later.")
            takes_strong_lock = True

        # --- backfills -----------------------------------------------------
        if _find(r"^\s*(UPDATE|DELETE)\b", S) and not _find(r"\bWHERE\b", S):
            add("long_running_backfill", "high", S,
                "Unbounded UPDATE/DELETE rewrites every row in one transaction: long lock hold, "
                "huge WAL, table bloat. Safe pattern: batch by primary key with sleeps.")
        elif _find(r"^\s*UPDATE\b", S) and _find(r"WHERE\s+[\w\"]+\s+IS\s+NULL", S):
            add("long_running_backfill", "medium", S,
                "Backfill over an unbounded predicate; batch it by primary key range instead.")

        # --- transactional legality -----------------------------------------
        if _find(r"CONCURRENTLY", S) and in_explicit_txn:
            add("non_transactional_in_txn", "critical", S,
                "CONCURRENTLY cannot run inside a transaction block; this migration will abort at runtime.")

    if takes_strong_lock and not has_lock_timeout:
        add("missing_lock_timeout", "medium", stmts[0] if stmts else "",
            "The migration acquires ACCESS EXCLUSIVE without SET lock_timeout, so it can queue behind a "
            "long-running query and freeze the table for everyone waiting behind it.")

    # de-duplicate identical (risk, statement) pairs
    seen, uniq = set(), []
    for f in findings:
        k = (f["risk"], f["statement"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)

    return {
        "statements": stmts,
        "statement_count": len(stmts),
        "in_explicit_transaction": in_explicit_txn,
        "has_lock_timeout": has_lock_timeout,
        "findings": uniq,
        "candidate_risks": sorted({f["risk"] for f in uniq}),
    }
