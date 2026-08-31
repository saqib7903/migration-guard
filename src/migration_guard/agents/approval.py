"""Human approval gate.

MigrationGuard is allowed to *read* a schema and to run migrations inside a database it
created and will drop. It is never allowed to apply anything anywhere else. This module
is the only code path that could touch a non-sandbox database, and it refuses to do so
without a recorded human decision.

Design notes
------------
* The agent cannot call this. It is a CLI verb a person runs.
* The approval record is written to disk before execution, so there is always an audit
  trail of who approved which exact SQL (sha256 of the statement text).
* A DSN that is not explicitly marked as a sandbox is rejected outright.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from ..config import SETTINGS


class ApprovalRequired(RuntimeError):
    pass


class NotASandbox(RuntimeError):
    pass


def sql_digest(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


def is_sandbox(dsn: str) -> bool:
    """Only DSNs the operator has explicitly declared safe are executable."""
    allow = [d.strip() for d in os.environ.get("MG_SANDBOX_DSNS", "").split(",") if d.strip()]
    return dsn in allow or dsn == SETTINGS.shadow_dsn


def record_approval(
    case_id: str, sql: str, approver: str, verdict_summary: str, dsn: str, out_dir: Path | None = None
) -> Path:
    out_dir = out_dir or (SETTINGS.runs_dir / "approvals")
    out_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {
        "case_id": case_id,
        "sql_sha256_16": sql_digest(sql),
        "sql": sql,
        "approver": approver,
        "verdict_summary": verdict_summary,
        "target_dsn": dsn.split("@")[-1],  # never store credentials
        "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    p = out_dir / f"{case_id}-{rec['sql_sha256_16']}.json"
    p.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return p


def guard(dsn: str, approver: str | None, sql: str, case_id: str, summary: str) -> Path:
    """Raise unless a human has approved this exact SQL against a declared sandbox."""
    if not is_sandbox(dsn):
        raise NotASandbox(
            f"refusing to execute against {dsn.split('@')[-1]}: not listed in MG_SANDBOX_DSNS. "
            "MigrationGuard only ever executes SQL inside a database it created, or a DSN you "
            "have explicitly declared disposable."
        )
    if not approver:
        raise ApprovalRequired(
            "a named human approver is required (--approved-by 'name <email>'). "
            f"SQL fingerprint {sql_digest(sql)}."
        )
    return record_approval(case_id, sql, approver, summary, dsn)
