"""Ablation R0: the deterministic linter alone, no model at all.

Kept as a first-class system because "would a linter have been enough?" is the first
question any reviewer should ask of an LLM-based tool, and because it is free to run.
"""
from __future__ import annotations

import time

from ..evidence import build_catalog
from ..models import SEVERITY_ORDER, Evidence, Finding, MigrationCase, Verdict
from ..tools import lock_model, static_rules
from ..tools.sqlsplit import split_statements
from ..trace import Tracer


def review(case: MigrationCase, llm=None, tracer: Tracer | None = None) -> Verdict:
    t0 = time.time()
    tracer = tracer or Tracer(label=f"rules_only {case.case_id}")
    lint = static_rules.lint(case.migration_sql)
    locks = lock_model.analyze(split_statements(case.migration_sql))
    tracer.tool_call("rules_only", "static_lint", {})
    tracer.tool_result("rules_only", "static_lint", lint.get("findings"))
    tracer.tool_call("rules_only", "lock_model", {})
    tracer.tool_result("rules_only", "lock_model", locks)

    cat = build_catalog(lint, locks, {"available": False})
    findings = [
        Finding(
            risk=f["risk"],
            severity=f["severity"],
            statement=f["statement"],
            rationale=f["rationale"],
            evidence=[Evidence("static_lint", f"rule {f['rule_id']}", {})],
        )
        for f in lint["findings"]
    ]
    worst = max((SEVERITY_ORDER.get(f.severity, 0) for f in findings), default=0)
    decision = "safe" if worst == 0 else ("block" if worst >= 4 else "needs_changes")
    v = Verdict(
        decision=decision,  # type: ignore[arg-type]
        findings=findings,
        summary=f"{len(findings)} rule hit(s); strongest lock {locks['strongest_lock']}",
        llm_calls=0,
        wall_seconds=round(time.time() - t0, 3),
        run_id=tracer.run_id,
        system="rules_only",
    )
    tracer.decision("rules_only", {"decision": decision, "risks": sorted(v.risks()),
                                   "evidence_ids": len(cat)})
    tracer.save_markdown()
    return v
