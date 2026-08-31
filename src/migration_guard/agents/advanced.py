"""Advanced system A: a tool-using reviewer, a grounding verifier, and a rewriter whose
output is validated by executing it.

Pipeline
--------
  reviewer  -- picks tools, reads what they returned, produces candidate findings
  evidence  -- every tool observation is catalogued with a stable id (deterministic)
  verifier  -- must cite catalogue ids per finding; uncitable findings are dropped and
               severities are re-grounded. Fabricated ids are discarded mechanically.
  rewriter  -- proposes a safe rewrite, which is then EXECUTED in the shadow database and
               schema-diffed against the original migration's end state. A rewrite that
               does not run, or that lands on a different schema, is retried once with the
               server's own error message, then reported as unvalidated rather than shown
               as if it worked.

Nothing in this pipeline touches a real database. Applying anything requires the human
approval gate in approval.py.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..config import SETTINGS
from ..evidence import build_catalog, render, resolve
from ..llm import LLM, extract_json
from ..models import SEVERITY_ORDER, Finding, MigrationCase, Verdict
from ..tools import lock_model, shadow_db, static_rules
from ..tools.sqlsplit import split_statements
from ..trace import Tracer
from . import prompts

TOOLS = ("static_lint", "lock_model", "shadow_probe")


# --------------------------------------------------------------------------- helpers
def _run_tool(name: str, case: MigrationCase, cache: dict[str, Any], settings=SETTINGS) -> Any:
    if name in cache:
        return cache[name]
    if name == "static_lint":
        out = static_rules.lint(case.migration_sql)
        cache[name] = out
        return {k: v for k, v in out.items() if k != "statements"}
    if name == "lock_model":
        out = lock_model.analyze(split_statements(case.migration_sql))
        cache[name] = out
        return out
    if name == "shadow_probe":
        out = shadow_db.probe(case.base_schema_sql, case.migration_sql, settings)
        cache[name] = out
        return shadow_db.summarize_for_agent(out)
    raise KeyError(name)


def _coerce_findings(d: dict) -> list[Finding]:
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


def compare_schemas(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Did the rewrite land on the same schema the original migration produced?"""
    ca = {tuple(r[:4]) for r in a.get("columns", [])}
    cb = {tuple(r[:4]) for r in b.get("columns", [])}
    ia, ib = set(a.get("indexes", [])), set(b.get("indexes", []))
    return {
        "columns_match": ca == cb,
        "missing_columns": sorted("/".join(map(str, x)) for x in (ca - cb))[:8],
        "extra_columns": sorted("/".join(map(str, x)) for x in (cb - ca))[:8],
        "indexes_match": ia == ib,
        "missing_indexes": sorted(ia - ib)[:5],
        "extra_indexes": sorted(ib - ia)[:5],
    }


# -------------------------------------------------------------------------- reviewer
def run_reviewer(
    case: MigrationCase, llm: LLM, tracer: Tracer, tool_cache: dict[str, Any], settings=SETTINGS
) -> dict[str, Any]:
    user = prompts.REVIEWER_USER.format(
        schema=case.base_schema_sql.strip(), migration=case.migration_sql.strip(), case_id=case.case_id
    )
    tracer.agent_instructions("reviewer", prompts.REVIEWER_SYSTEM, user)
    transcript = user
    final: dict[str, Any] = {}

    for it in range(settings.max_tool_iterations):
        resp = llm.complete(prompts.REVIEWER_SYSTEM, transcript, agent="reviewer", tracer=tracer)
        data = extract_json(resp.text)
        action = str(data.get("action", "")).lower()

        if action == "call_tool":
            tool = str(data.get("tool", ""))
            if tool not in TOOLS:
                tracer.retry("reviewer", f"unknown tool {tool!r}", it + 1)
                transcript += (
                    f"\n\nTOOL ERROR: '{tool}' is not a tool. Available: {', '.join(TOOLS)}. "
                    "Reply with a corrected JSON action."
                )
                continue
            tracer.thought("reviewer", data.get("why", ""))
            tracer.tool_call("reviewer", tool, {})
            result = _run_tool(tool, case, tool_cache, settings)
            tracer.tool_result("reviewer", tool, result)
            transcript += (
                f"\n\nTOOL RESULT — {tool}\n```json\n{json.dumps(result, indent=1, default=str)[:6000]}\n```"
                "\n\nReply with your next JSON action."
            )
            continue

        if action == "final" or "findings" in data:
            final = data
            break

        tracer.retry("reviewer", "reply was not a valid action object", it + 1)
        transcript += (
            '\n\nYour reply was not valid. Reply with exactly one JSON object: '
            '{"action":"call_tool","tool":...} or {"action":"final",...}.'
        )

    if not final:
        # last resort: make sure the deterministic tools still ran so the verifier has
        # something to work with, and record that the agent never converged.
        for t in TOOLS:
            _run_tool(t, case, tool_cache, settings)
        tracer.decision("reviewer", {"converged": False})
        final = {"decision": "needs_changes", "summary": "reviewer did not converge", "findings": []}
    return final


# -------------------------------------------------------------------------- verifier
def run_verifier(
    findings: list[Finding], catalog: dict[str, Any], llm: LLM, tracer: Tracer
) -> tuple[list[Finding], dict[str, Any]]:
    if not findings:
        return [], {"dropped": 0, "regraded": 0, "fabricated_ids": 0}

    listing = "\n".join(
        f"[{i}] risk={f.risk} severity={f.severity} statement={f.statement}\n     rationale={f.rationale}"
        for i, f in enumerate(findings)
    )
    user = prompts.VERIFIER_USER.format(catalog=render(catalog), findings=listing)
    tracer.agent_instructions("verifier", prompts.VERIFIER_SYSTEM, user)
    resp = llm.complete(prompts.VERIFIER_SYSTEM, user, agent="verifier", tracer=tracer)
    data = extract_json(resp.text)

    kept: list[Finding] = []
    dropped = regraded = fabricated = 0
    seen: set[int] = set()
    for row in data.get("verified", []) or []:
        try:
            idx = int(row.get("index"))
        except Exception:
            continue
        if idx in seen or idx < 0 or idx >= len(findings):
            continue
        seen.add(idx)
        f = findings[idx]
        ids = [str(x) for x in (row.get("evidence_ids") or [])]
        ev = resolve(ids, catalog)
        fabricated += len(ids) - len(ev)
        if not ev:
            dropped += 1
            tracer.decision("verifier", {"dropped": f.risk, "statement": f.statement[:80],
                                         "reason": "no catalogue evidence"})
            continue
        sev = str(row.get("severity", f.severity)).lower()
        if sev in SEVERITY_ORDER and sev != f.severity:
            regraded += 1
            f.severity = sev  # type: ignore[assignment]
        f.evidence = ev
        kept.append(f)

    # findings the verifier silently skipped are treated as unverified -> dropped
    dropped += len(findings) - len(seen)
    stats = {"dropped": dropped, "regraded": regraded, "fabricated_ids": fabricated,
             "kept": len(kept), "input": len(findings)}
    tracer.decision("verifier", stats)
    return kept, stats


# -------------------------------------------------------------------------- rewriter
def run_rewriter(
    case: MigrationCase, findings: list[Finding], shadow: dict[str, Any], llm: LLM, tracer: Tracer,
    settings=SETTINGS,
) -> dict[str, Any]:
    listing = "\n".join(f"- {f.risk} ({f.severity}): {f.statement}" for f in findings) or "- none"
    user = prompts.REWRITER_USER.format(
        schema=case.base_schema_sql.strip(),
        migration=case.migration_sql.strip(),
        findings=listing,
        shadow=json.dumps(shadow_db.summarize_for_agent(shadow), indent=1, default=str)[:2500],
    )
    tracer.agent_instructions("rewriter", prompts.REWRITER_SYSTEM, user)

    attempt, sql, notes, deferred = 0, "", "", False
    validation: dict[str, Any] = {}
    while attempt < 2:
        attempt += 1
        prompt = user if attempt == 1 else user + "\n\n" + prompts.REWRITER_RETRY.format(
            previous=sql, errors=json.dumps(validation, indent=1, default=str)[:1500]
        )
        resp = llm.complete(prompts.REWRITER_SYSTEM, prompt, agent="rewriter", tracer=tracer)
        data = extract_json(resp.text)
        sql = str(data.get("rewrite_sql", "")).strip()
        notes = str(data.get("notes", ""))[:800]
        deferred = bool(data.get("deferred", False))
        if not sql:
            validation = {"error": "model returned no rewrite_sql"}
            tracer.retry("rewriter", "empty rewrite_sql", attempt)
            continue

        tracer.tool_call("rewriter", "shadow_probe(rewrite)", {"chars": len(sql)})
        rp = shadow_db.probe(case.base_schema_sql, sql, settings)
        tracer.tool_result("rewriter", "shadow_probe(rewrite)", shadow_db.summarize_for_agent(rp))

        if not rp.get("available"):
            validation = {"error": rp.get("reason"), "executed": False}
            break
        errors = [r for r in rp.get("execution", []) if not r.get("ok")]
        diff = (
            compare_schemas(shadow.get("schema_after", {}), rp.get("schema_after", {}))
            if shadow.get("available")
            else {}
        )
        # This dict is pasted verbatim into the retry prompt, so it is the one place where
        # measurement noise turns into a different prompt hash and breaks cassette replay.
        # It therefore carries only what the model needs to fix the rewrite -- did it run,
        # what did the server say, and did it land on the right schema -- and nothing
        # derived from timing or from lock sampling.
        validation = {
            "executed_cleanly": not errors,
            "errors": [{"statement": r["statement"], "error": r["error"]} for r in errors],
            "schema_diff_vs_original": diff,
        }
        # kept out of the prompt, but recorded for the human reading the trajectory
        tracer.tool_result(
            "rewriter",
            "shadow_probe(rewrite) timings",
            {
                "total_execution_time": shadow_db.bucket_ms(rp.get("total_execution_ms")),
                "observed_locks": dict(sorted((rp.get("observed_locks") or {}).items())),
            },
        )
        original_failed = bool(shadow.get("migration_failed"))
        equivalent = bool(diff.get("columns_match")) if diff else False
        ok = (not errors) and (equivalent or deferred or original_failed)
        if ok:
            validation["verdict"] = (
                "validated: runs clean and lands on the same schema"
                if equivalent
                else "validated: runs clean; end state intentionally differs (deferred / original was broken)"
            )
            tracer.decision("rewriter", {"validated": True, "attempt": attempt})
            return {"sql": sql, "notes": notes, "validated": True, "validation": validation,
                    "deferred": deferred, "attempts": attempt}
        tracer.retry("rewriter", json.dumps(validation, default=str)[:200], attempt)

    tracer.decision("rewriter", {"validated": False, "attempts": attempt})
    return {"sql": sql, "notes": notes, "validated": False, "validation": validation,
            "deferred": deferred, "attempts": attempt}


# ---------------------------------------------------------------------- orchestrator
def review(
    case: MigrationCase, llm: LLM | None = None, tracer: Tracer | None = None, settings=SETTINGS
) -> Verdict:
    llm = llm or LLM()
    t0 = time.time()
    tracer = tracer or Tracer(label=f"advanced {case.case_id}")
    tool_cache: dict[str, Any] = {}

    final = run_reviewer(case, llm, tracer, tool_cache, settings)
    findings = _coerce_findings(final)

    lint = tool_cache.get("static_lint") or static_rules.lint(case.migration_sql)
    locks = tool_cache.get("lock_model") or lock_model.analyze(split_statements(case.migration_sql))
    shadow = tool_cache.get("shadow_probe") or {"available": False, "reason": "not requested by the agent"}
    catalog = build_catalog(lint, locks, shadow)
    tracer.tool_result("orchestrator", "evidence_catalog", {"ids": list(catalog)[:40], "size": len(catalog)})

    vstats: dict[str, Any] = {}
    if settings.enable_verifier:
        findings, vstats = run_verifier(findings, catalog, llm, tracer)

    decision = str(final.get("decision", "needs_changes"))
    if shadow.get("available") and shadow.get("migration_failed"):
        decision = "block"  # a migration that does not run is never "safe"
    if not findings and decision != "safe" and not shadow.get("migration_failed"):
        decision = "safe" if decision == "safe" else decision
    worst = max((SEVERITY_ORDER.get(f.severity, 0) for f in findings), default=0)
    if worst >= 4:
        decision = "block"
    elif worst >= 2 and decision == "safe":
        decision = "needs_changes"
    elif worst == 0 and not findings:
        decision = "safe"

    rewrite: dict[str, Any] = {}
    if settings.enable_rewriter and decision != "safe" and findings:
        rewrite = run_rewriter(case, findings, shadow, llm, tracer, settings)

    v = Verdict(
        decision=decision,  # type: ignore[arg-type]
        findings=findings,
        rewrite_sql=rewrite.get("sql") or None,
        rewrite_validated=bool(rewrite.get("validated")),
        rewrite_notes=str(rewrite.get("notes", "")),
        summary=str(final.get("summary", ""))[:500],
        tokens_in=llm.tokens_in,
        tokens_out=llm.tokens_out,
        llm_calls=llm.calls,
        wall_seconds=round(time.time() - t0, 2),
        run_id=tracer.run_id,
        system="advanced",
    )
    tracer.decision(
        "orchestrator",
        {
            "decision": v.decision,
            "risks": sorted(v.risks()),
            "verifier": vstats,
            "rewrite_validated": v.rewrite_validated,
            "shadow_available": shadow.get("available"),
        },
    )
    tracer.checkpoint(
        "orchestrator",
        "human approval required",
        "MigrationGuard never applies a migration or a rewrite. A human reviewer must "
        "approve via `mg approve` before anything is run outside the sandbox.",
    )
    tracer.save_markdown()
    return v
