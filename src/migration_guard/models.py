"""Typed result objects shared by the baseline agent, the advanced pipeline and the eval harness."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["none", "low", "medium", "high", "critical"]
SEVERITY_ORDER: dict[str, int] = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# The closed vocabulary of risk classes. Both the baseline and the advanced system must
# emit labels from this list, otherwise scoring would degrade into fuzzy string matching.
RISK_TAXONOMY: dict[str, str] = {
    "table_rewrite": "Statement rewrites the whole table (full copy) while holding a lock.",
    # NB: there is deliberately no `access_exclusive_lock` label. Taking ACCESS EXCLUSIVE
    # is the *mechanism* behind almost every label below, not a separate risk, and having
    # it in the vocabulary let the reviewer answer every question with it. See the
    # improvement changelog, entry A-5.
    "blocking_index_build": "CREATE/DROP INDEX without CONCURRENTLY blocks writes for the build.",
    "not_null_without_default": "ADD COLUMN NOT NULL with no default, or SET NOT NULL, fails or scans the table.",
    "destructive_drop": "DROP COLUMN/TABLE destroys data and breaks running application code.",
    "unsafe_type_change": "ALTER COLUMN TYPE that is not binary-coercible, forcing a rewrite.",
    "validating_constraint": "ADD CONSTRAINT ... (not NOT VALID) scans and locks the table.",
    "missing_lock_timeout": "Migration does not set lock_timeout, so it can queue behind traffic and stall everything.",
    "long_running_backfill": "UPDATE/DELETE touching the whole table in one transaction; long lock + bloat.",
    "non_transactional_in_txn": "CONCURRENTLY / VACUUM style statement that cannot run inside a transaction block.",
    "renaming_in_use": "RENAME of a column/table still referenced by deployed application code.",
    "migration_will_fail": "The migration does not run at all against this schema and data "
                           "(syntax error, missing object, dependency, or a constraint the "
                           "existing rows violate). Deploy aborts, possibly half-applied.",
}


@dataclass
class Evidence:
    """A pointer from a claim back to something a tool actually observed."""

    source: str  # tool name
    detail: str
    ref: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    risk: str  # key in RISK_TAXONOMY
    severity: Severity
    statement: str  # the offending SQL statement (trimmed)
    rationale: str
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return len(self.evidence) > 0


@dataclass
class Verdict:
    decision: Literal["safe", "needs_changes", "block"]
    findings: list[Finding] = field(default_factory=list)
    rewrite_sql: str | None = None
    rewrite_validated: bool = False
    rewrite_notes: str = ""
    summary: str = ""
    # bookkeeping
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    wall_seconds: float = 0.0
    run_id: str = ""
    system: str = ""  # "baseline" | "advanced"

    def risks(self) -> set[str]:
        return {f.risk for f in self.findings}

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class MigrationCase:
    """One unit of work: the migration under review plus the schema it lands on."""

    case_id: str
    title: str
    base_schema_sql: str
    migration_sql: str
    # ground truth (eval only)
    expected_risks: list[str] = field(default_factory=list)
    expected_decision: str = ""
    notes: str = ""
