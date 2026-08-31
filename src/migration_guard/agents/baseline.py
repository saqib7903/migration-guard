"""Baseline system B0: one LLM call, no tools, no verification.

This is the honest version of what most teams actually do today ("paste the migration
into a chat window and ask if it's safe"). It gets the same model, the same temperature,
the same risk taxonomy and the same output contract as the advanced system, so the
comparison isolates the agent architecture rather than the prompt.
"""
from __future__ import annotations

import time

from ..llm import LLM, extract_json
from ..models import Finding, MigrationCase, Verdict
from ..trace import Tracer
from . import prompts


def _coerce(d: dict) -> list[Finding]:
    out = []
    for f in d.get("findings", []) or []:
        if not isinstance(f, dict):
            continue
        out.append(
            Finding(
                risk=str(f.get("risk", "")).strip(),
                severity=str(f.get("severity", "medium")).strip().lower(),
                statement=str(f.get("statement", ""))[:240],
                rationale=str(f.get("rationale", ""))[:600],
            )
        )
    return out


def review(case: MigrationCase, llm: LLM | None = None, tracer: Tracer | None = None) -> Verdict:
    llm = llm or LLM()
    t0 = time.time()
    tracer = tracer or Tracer(label=f"baseline {case.case_id}")

    user = prompts.BASELINE_USER.format(
        schema=case.base_schema_sql.strip(), migration=case.migration_sql.strip(), case_id=case.case_id
    )
    tracer.agent_instructions("baseline", prompts.BASELINE_SYSTEM, user)
    resp = llm.complete(prompts.BASELINE_SYSTEM, user, agent="baseline", tracer=tracer)
    data = extract_json(resp.text)

    v = Verdict(
        decision=str(data.get("decision", "needs_changes")),
        findings=_coerce(data),
        summary=str(data.get("summary", ""))[:500],
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
        llm_calls=1,
        wall_seconds=round(time.time() - t0, 2),
        run_id=tracer.run_id,
        system="baseline",
    )
    tracer.decision("baseline", {"decision": v.decision, "risks": sorted(v.risks())})
    tracer.save_markdown()
    return v
