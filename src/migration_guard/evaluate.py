"""Evaluation harness and metrics.

Every number in the README is produced here. The scoring rules are deliberately blunt and
written out in full so a judge can disagree with them explicitly:

  risk recall     : (predicted risks ∩ labelled risks) / labelled risks, micro-averaged
  risk precision  : (predicted risks ∩ labelled risks) / predicted risks, micro-averaged
  decision acc.   : exact match on safe / needs_changes / block
  outage misses   : labelled-unsafe cases the system called "safe" -- the failure that
                    actually takes a database down, counted separately because a single
                    one of these outweighs a lot of precision
  false alarms    : risks reported on the cases labelled safe
  ungrounded      : findings carrying no tool evidence (baseline: all of them)
  rewrite validity: share of unsafe cases where the proposed rewrite executed cleanly in
                    the shadow database and landed on an equivalent schema
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import SETTINGS
from .llm import LLM
from .models import SEVERITY_ORDER, MigrationCase, Verdict
from .trace import Tracer, new_run_id

SYSTEMS: dict[str, Callable[..., Verdict]] = {}


def _register() -> None:
    from .agents import advanced, baseline, rules_only

    SYSTEMS["rules_only"] = rules_only.review
    SYSTEMS["baseline"] = baseline.review
    SYSTEMS["advanced"] = advanced.review


_register()


@dataclass
class CaseResult:
    case_id: str
    system: str
    expected_risks: list[str]
    predicted_risks: list[str]
    expected_decision: str
    predicted_decision: str
    true_positives: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)
    ungrounded_findings: int = 0
    total_findings: int = 0
    rewrite_offered: bool = False
    rewrite_validated: bool = False
    llm_calls: int = 0
    wall_seconds: float = 0.0
    run_id: str = ""
    error: str = ""


# Only findings at or above this severity count as "risks the system reported". A
# finding graded `low` is informational ("this takes ACCESS EXCLUSIVE for 1.7ms") and
# nobody blocks a deploy on it. The threshold is applied identically to every system.
MIN_SCORED_SEVERITY = os.environ.get("MG_MIN_SEVERITY", "medium")


def score_case(case: MigrationCase, v: Verdict) -> CaseResult:
    floor = SEVERITY_ORDER.get(MIN_SCORED_SEVERITY, 2)
    exp = set(case.expected_risks)
    pred = {f.risk for f in v.findings if SEVERITY_ORDER.get(f.severity, 0) >= floor}
    return CaseResult(
        case_id=case.case_id,
        system=v.system,
        expected_risks=sorted(exp),
        predicted_risks=sorted(pred),
        expected_decision=case.expected_decision,
        predicted_decision=v.decision,
        true_positives=sorted(exp & pred),
        false_positives=sorted(pred - exp),
        false_negatives=sorted(exp - pred),
        ungrounded_findings=sum(1 for f in v.findings if not f.supported),
        total_findings=len(v.findings),
        rewrite_offered=bool(v.rewrite_sql),
        rewrite_validated=v.rewrite_validated,
        llm_calls=v.llm_calls,
        wall_seconds=v.wall_seconds,
        run_id=v.run_id,
    )


def aggregate(results: list[CaseResult], cases: dict[str, MigrationCase]) -> dict[str, Any]:
    tp = sum(len(r.true_positives) for r in results)
    fp = sum(len(r.false_positives) for r in results)
    fn = sum(len(r.false_negatives) for r in results)
    unsafe = [r for r in results if cases[r.case_id].expected_decision != "safe"]
    safe = [r for r in results if cases[r.case_id].expected_decision == "safe"]
    offered = [r for r in unsafe if r.rewrite_offered]
    return {
        "cases": len(results),
        "risk_recall": round(tp / (tp + fn), 3) if (tp + fn) else 0.0,
        "risk_precision": round(tp / (tp + fp), 3) if (tp + fp) else 0.0,
        "risk_f1": round(2 * tp / (2 * tp + fp + fn), 3) if tp else 0.0,
        "decision_accuracy": round(
            sum(1 for r in results if r.predicted_decision == r.expected_decision) / len(results), 3
        ) if results else 0.0,
        "outage_misses": sum(1 for r in unsafe if r.predicted_decision == "safe"),
        "false_alarm_risks_on_safe_cases": sum(len(r.predicted_risks) for r in safe),
        "ungrounded_findings": sum(r.ungrounded_findings for r in results),
        "total_findings": sum(r.total_findings for r in results),
        "rewrites_offered": len(offered),
        "rewrites_validated": sum(1 for r in offered if r.rewrite_validated),
        "rewrite_validity": round(
            sum(1 for r in offered if r.rewrite_validated) / len(offered), 3
        ) if offered else 0.0,
        "llm_calls": sum(r.llm_calls for r in results),
        "wall_seconds": round(sum(r.wall_seconds for r in results), 1),
        "errors": sum(1 for r in results if r.error),
    }


NEEDS_SHADOW = ("advanced",)


def preflight(systems: list[str], settings=SETTINGS) -> str | None:
    """Refuse to produce meaningless numbers.

    The recorded cassettes were made with a working shadow database, so the reviewer's
    prompt contains real tool output. Without a database the tool returns
    `{"available": false}`, the prompt differs, every cassette misses, and the advanced
    system scores 0.0 across the board -- which looks like a broken project rather than a
    missing dependency. Fail loudly here instead.
    """
    if not any(s in NEEDS_SHADOW for s in systems):
        return None
    from .tools import shadow_db

    if not shadow_db.HAVE_PSYCOPG:
        return "psycopg is not installed. Run: pip install -r requirements.txt"
    if not settings.shadow_enabled:
        return (
            "MG_SHADOW_ENABLED=0, but the `advanced` system's recorded cassettes were made "
            "with a live shadow database. Re-enable it, or evaluate only "
            "`--systems rules_only,baseline`."
        )
    p = shadow_db.probe("CREATE TABLE mg_preflight (id serial PRIMARY KEY, v text);",
                        "ALTER TABLE mg_preflight ADD COLUMN w text;", settings)
    if p.get("available"):
        return None
    return (
        f"the shadow database at {settings.shadow_dsn.split('@')[-1]} is not reachable "
        f"({p.get('reason')}).\n\n"
        "  The `advanced` system executes migrations for real, so it cannot be evaluated\n"
        "  without one, and its cassettes will not replay. Start it with:\n\n"
        "      make db-up            # needs Docker Desktop running\n\n"
        "  or point MG_SHADOW_DSN at any throwaway PostgreSQL 16 you can create databases\n"
        "  on. To skip it entirely: mg eval --systems rules_only,baseline"
    )


def run_system(
    system: str, cases: list[MigrationCase], settings=SETTINGS, run_id: str | None = None
) -> tuple[list[CaseResult], dict[str, Any], float]:
    fn = SYSTEMS[system]
    run_id = run_id or new_run_id(f"eval-{system}")
    cost = 0.0

    def one(case: MigrationCase) -> tuple[CaseResult, float]:
        llm = LLM(settings)
        tracer = Tracer(run_id=f"{run_id}/{case.case_id}", label=f"{system} · {case.case_id}")
        try:
            v = fn(case, llm=llm, tracer=tracer)
            return score_case(case, v), getattr(llm, "cost_usd", 0.0)
        except Exception as e:  # a crashed case is a failed case, not a skipped one
            return (
                CaseResult(
                    case_id=case.case_id, system=system, expected_risks=case.expected_risks,
                    predicted_risks=[], expected_decision=case.expected_decision,
                    predicted_decision="ERROR", error=f"{type(e).__name__}: {e}",
                    false_negatives=sorted(case.expected_risks), run_id=tracer.run_id,
                ),
                getattr(llm, "cost_usd", 0.0),
            )

    workers = int(__import__("os").environ.get("MG_EVAL_WORKERS", "1"))
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as ex:
            pairs = list(ex.map(one, cases))
    else:
        pairs = [one(c) for c in cases]
    results = [p[0] for p in pairs]
    cost = sum(p[1] for p in pairs)
    agg = aggregate(results, {c.case_id: c for c in cases})
    agg["system"] = system
    agg["estimated_cost_usd"] = round(cost, 4)
    agg["run_id"] = run_id
    return results, agg, cost


def compare(
    systems: list[str], cases: list[MigrationCase], settings=SETTINGS, out_dir: Path | None = None
) -> dict[str, Any]:
    out_dir = out_dir or settings.reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report: dict[str, Any] = {
        "generated_at": stamp,
        "model": settings.model,
        "provider": settings.provider,
        "seed_rows": settings.seed_rows,
        "case_count": len(cases),
        "unsafe_cases": sum(1 for c in cases if c.expected_decision != "safe"),
        "safe_cases": sum(1 for c in cases if c.expected_decision == "safe"),
        "systems": {},
    }
    for s in systems:
        results, agg, _ = run_system(s, cases, settings)
        report["systems"][s] = {"summary": agg, "cases": [asdict(r) for r in results]}
        print(f"  {s:10s} recall={agg['risk_recall']} precision={agg['risk_precision']} "
              f"decision={agg['decision_accuracy']} outage_misses={agg['outage_misses']} "
              f"cost=${agg['estimated_cost_usd']}")
    (out_dir / "comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "comparison.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(report: dict[str, Any]) -> str:
    rows = [
        ("Risk recall", "risk_recall"),
        ("Risk precision", "risk_precision"),
        ("Risk F1", "risk_f1"),
        ("Decision accuracy", "decision_accuracy"),
        ("Outage misses (unsafe called safe)", "outage_misses"),
        ("False-alarm risks on safe cases", "false_alarm_risks_on_safe_cases"),
        ("Findings with no tool evidence", "ungrounded_findings"),
        ("Rewrites offered", "rewrites_offered"),
        ("Rewrites validated by execution", "rewrites_validated"),
        ("LLM calls", "llm_calls"),
        ("Wall seconds", "wall_seconds"),
        ("Estimated cost (USD)", "estimated_cost_usd"),
        ("Harness errors", "errors"),
    ]
    systems = list(report["systems"])
    out = [
        "# MigrationGuard — baseline vs advanced",
        "",
        f"- generated: `{report['generated_at']}`",
        f"- model: `{report['model']}` · provider: `{report['provider']}` · "
        f"shadow rows/table: `{report['seed_rows']}`",
        f"- cases: **{report['case_count']}** ({report['unsafe_cases']} labelled unsafe, "
        f"{report['safe_cases']} labelled safe)",
        "",
        "| Metric | " + " | ".join(systems) + " |",
        "|---|" + "---|" * len(systems),
    ]
    for label, key in rows:
        out.append(
            f"| {label} | " + " | ".join(str(report["systems"][s]["summary"].get(key, "")) for s in systems) + " |"
        )
    out += ["", "## Per-case decisions", "",
            "| Case | expected | " + " | ".join(systems) + " |",
            "|---|---|" + "---|" * len(systems)]
    ids = [c["case_id"] for c in report["systems"][systems[0]]["cases"]]
    for cid in ids:
        cells = []
        exp = ""
        for s in systems:
            row = next(c for c in report["systems"][s]["cases"] if c["case_id"] == cid)
            exp = row["expected_decision"]
            mark = "✅" if row["predicted_decision"] == exp else "❌"
            cells.append(f"{mark} {row['predicted_decision']}")
        out.append(f"| `{cid}` | {exp} | " + " | ".join(cells) + " |")
    out += ["", "## Missed risks (false negatives) per system", ""]
    for s in systems:
        misses = {
            c["case_id"]: c["false_negatives"] for c in report["systems"][s]["cases"] if c["false_negatives"]
        }
        out.append(f"**{s}** — {sum(len(v) for v in misses.values())} missed risk(s)")
        for cid, m in misses.items():
            out.append(f"- `{cid}`: {', '.join(m)}")
        out.append("")
    return "\n".join(out)
