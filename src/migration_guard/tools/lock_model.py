"""PostgreSQL lock model: which lock a DDL statement takes, and what that lock blocks.

This is a *deterministic knowledge tool*, not a guess. The conflict matrix is the one
documented in the PostgreSQL manual (Explicit Locking / Table-level locks). The agent is
required to consult it before it is allowed to claim "this blocks reads".
"""
from __future__ import annotations

import re
from typing import Any

MODES = [
    "ACCESS SHARE",
    "ROW SHARE",
    "ROW EXCLUSIVE",
    "SHARE UPDATE EXCLUSIVE",
    "SHARE",
    "SHARE ROW EXCLUSIVE",
    "EXCLUSIVE",
    "ACCESS EXCLUSIVE",
]

# conflicts[a] = set of modes that conflict with a (PostgreSQL table-level lock matrix)
CONFLICTS: dict[str, set[str]] = {
    "ACCESS SHARE": {"ACCESS EXCLUSIVE"},
    "ROW SHARE": {"EXCLUSIVE", "ACCESS EXCLUSIVE"},
    "ROW EXCLUSIVE": {"SHARE", "SHARE ROW EXCLUSIVE", "EXCLUSIVE", "ACCESS EXCLUSIVE"},
    "SHARE UPDATE EXCLUSIVE": {
        "SHARE UPDATE EXCLUSIVE", "SHARE", "SHARE ROW EXCLUSIVE", "EXCLUSIVE", "ACCESS EXCLUSIVE",
    },
    "SHARE": {"ROW EXCLUSIVE", "SHARE UPDATE EXCLUSIVE", "SHARE ROW EXCLUSIVE", "EXCLUSIVE", "ACCESS EXCLUSIVE"},
    "SHARE ROW EXCLUSIVE": {
        "ROW EXCLUSIVE", "SHARE UPDATE EXCLUSIVE", "SHARE", "SHARE ROW EXCLUSIVE", "EXCLUSIVE", "ACCESS EXCLUSIVE",
    },
    "EXCLUSIVE": {
        "ROW SHARE", "ROW EXCLUSIVE", "SHARE UPDATE EXCLUSIVE", "SHARE", "SHARE ROW EXCLUSIVE",
        "EXCLUSIVE", "ACCESS EXCLUSIVE",
    },
    "ACCESS EXCLUSIVE": set(MODES),
}

# what ordinary application traffic needs
TRAFFIC = {
    "SELECT": "ACCESS SHARE",
    "SELECT FOR UPDATE": "ROW SHARE",
    "INSERT/UPDATE/DELETE": "ROW EXCLUSIVE",
}

# statement pattern -> lock mode taken on the target relation
_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY", re.I), "SHARE UPDATE EXCLUSIVE",
     "CREATE INDEX CONCURRENTLY takes SHARE UPDATE EXCLUSIVE: writes keep working."),
    (re.compile(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\b", re.I), "SHARE",
     "CREATE INDEX takes SHARE: it blocks INSERT/UPDATE/DELETE for the whole build."),
    (re.compile(r"^\s*DROP\s+INDEX\s+CONCURRENTLY", re.I), "SHARE UPDATE EXCLUSIVE",
     "DROP INDEX CONCURRENTLY takes SHARE UPDATE EXCLUSIVE."),
    (re.compile(r"^\s*DROP\s+INDEX\b", re.I), "ACCESS EXCLUSIVE", "DROP INDEX takes ACCESS EXCLUSIVE."),
    (re.compile(r"^\s*ALTER\s+TABLE\b.*\bVALIDATE\s+CONSTRAINT\b", re.I | re.S), "SHARE UPDATE EXCLUSIVE",
     "VALIDATE CONSTRAINT takes SHARE UPDATE EXCLUSIVE and scans without blocking writes."),
    (re.compile(r"^\s*ALTER\s+TABLE\b", re.I), "ACCESS EXCLUSIVE",
     "Most ALTER TABLE forms take ACCESS EXCLUSIVE: every reader and writer queues behind it."),
    (re.compile(r"^\s*DROP\s+TABLE\b", re.I), "ACCESS EXCLUSIVE", "DROP TABLE takes ACCESS EXCLUSIVE."),
    (re.compile(r"^\s*TRUNCATE\b", re.I), "ACCESS EXCLUSIVE", "TRUNCATE takes ACCESS EXCLUSIVE."),
    (re.compile(r"^\s*CLUSTER\b|^\s*VACUUM\s+FULL\b|^\s*REINDEX\s+TABLE\b(?!.*CONCURRENTLY)", re.I),
     "ACCESS EXCLUSIVE", "CLUSTER / VACUUM FULL / REINDEX TABLE rewrite the table under ACCESS EXCLUSIVE."),
    (re.compile(r"^\s*(UPDATE|DELETE|INSERT)\b", re.I), "ROW EXCLUSIVE",
     "DML takes ROW EXCLUSIVE; it does not block readers but holds row locks for the transaction."),
    (re.compile(r"^\s*CREATE\s+TABLE\b", re.I), "NONE", "CREATE TABLE does not lock an existing relation."),
]


def lock_for(statement: str) -> dict[str, Any]:
    for pat, mode, why in _RULES:
        if pat.search(statement):
            return {"mode": mode, "why": why}
    return {"mode": "UNKNOWN", "why": "no lock rule matched this statement"}


def blocked_traffic(mode: str) -> dict[str, bool]:
    """Given a lock mode held by the migration, what ordinary traffic is blocked?"""
    if mode in ("NONE", "UNKNOWN"):
        return {k: False for k in TRAFFIC}
    return {name: (mode in CONFLICTS[need]) for name, need in TRAFFIC.items()}


def analyze(statements: list[str]) -> dict[str, Any]:
    """Tool entrypoint used by the agent."""
    rows = []
    for s in statements:
        info = lock_for(s)
        rows.append(
            {
                "statement": s[:220],
                "lock_mode": info["mode"],
                "explanation": info["why"],
                "blocks": blocked_traffic(info["mode"]),
            }
        )
    worst = "NONE"
    for r in rows:
        if r["lock_mode"] in MODES and (worst == "NONE" or MODES.index(r["lock_mode"]) > MODES.index(worst)):
            worst = r["lock_mode"]
    return {
        "per_statement": rows,
        "strongest_lock": worst,
        "blocks_reads": any(r["blocks"]["SELECT"] for r in rows),
        "blocks_writes": any(r["blocks"]["INSERT/UPDATE/DELETE"] for r in rows),
        "source": "PostgreSQL 16 documented table-level lock conflict matrix",
    }
