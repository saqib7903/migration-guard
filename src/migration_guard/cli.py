"""mg — MigrationGuard command line.

  mg review    --schema s.sql --migration m.sql [--system advanced]
  mg eval      [--systems rules_only,baseline,advanced] [--cases a,b]
  mg trajectory <run_id>
  mg approve   --case-id X --sql-file f.sql --approved-by "Name <email>" --dsn ...
  mg doctor
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import SETTINGS
from .dataset import load_case_from_files, load_cases
from .llm import LLM
from .models import MigrationCase
from .trace import Tracer


def _print_verdict(v, case: MigrationCase) -> None:
    colour = {"safe": "\033[32m", "needs_changes": "\033[33m", "block": "\033[31m"}.get(v.decision, "")
    print(f"\n{colour}● {v.decision.upper()}\033[0m  {case.case_id} — {v.summary}\n")
    for f in v.findings:
        print(f"  [{f.severity:8s}] {f.risk}")
        print(f"             {f.statement[:110]}")
        print(f"             {f.rationale[:160]}")
        for e in f.evidence:
            print(f"             └─ evidence [{e.source}] {e.detail[:120]}")
        print()
    if v.rewrite_sql:
        tag = "VALIDATED IN SHADOW DB" if v.rewrite_validated else "NOT VALIDATED — do not trust"
        print(f"  ── proposed rewrite ({tag}) ─────────────────────────────")
        for line in v.rewrite_sql.splitlines():
            print("  " + line)
        if v.rewrite_notes:
            print(f"\n  notes: {v.rewrite_notes}")
    print(f"\n  run: {v.run_id}  ·  llm calls: {v.llm_calls}  ·  {v.wall_seconds}s")
    print("  MigrationGuard does not apply migrations. Use `mg approve` for the human gate.\n")


def cmd_review(args: argparse.Namespace) -> int:
    from .agents import advanced, baseline, rules_only

    rc = _preflight_or_exit([args.system])
    if rc is not None:
        return rc
    fn = {"advanced": advanced.review, "baseline": baseline.review, "rules_only": rules_only.review}[args.system]
    if args.case:
        case = next(c for c in load_cases([args.case]))
    else:
        case = load_case_from_files(Path(args.schema), Path(args.migration))
    tracer = Tracer(label=f"{args.system} · {case.case_id}")
    v = fn(case, llm=LLM(), tracer=tracer)
    _print_verdict(v, case)
    if args.json:
        Path(args.json).write_text(json.dumps(v.to_dict(), indent=2, default=str), encoding="utf-8")
    return 0 if v.decision == "safe" else (1 if v.decision == "needs_changes" else 2)


def _preflight_or_exit(systems: list[str]) -> int | None:
    from .evaluate import preflight

    problem = preflight(systems, SETTINGS)
    if problem:
        print(f"\n\033[31mPREFLIGHT FAILED\033[0m {problem}\n")
        return 4
    return None


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluate import compare

    cases = load_cases(args.cases.split(",") if args.cases else None)
    systems = args.systems.split(",")
    rc = _preflight_or_exit(systems)
    if rc is not None:
        return rc
    print(f"evaluating {len(cases)} cases · systems={systems} · provider={SETTINGS.provider} "
          f"· model={SETTINGS.model}")
    report = compare(systems, cases, SETTINGS)
    print(f"\nwrote {SETTINGS.reports_dir / 'comparison.md'}")
    return 0


def cmd_trajectory(args: argparse.Namespace) -> int:
    p = SETTINGS.runs_dir / args.run_id
    md = p / "trajectory.md"
    if not md.exists():
        t = Tracer(run_id=args.run_id)
        md.write_text(t.render_markdown(), encoding="utf-8")
    print(md.read_text(encoding="utf-8"))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from .agents.approval import ApprovalRequired, NotASandbox, guard

    sql = Path(args.sql_file).read_text(encoding="utf-8")
    try:
        p = guard(args.dsn, args.approved_by, sql, args.case_id, args.summary)
    except (ApprovalRequired, NotASandbox) as e:
        print(f"\033[31mREFUSED\033[0m {e}")
        return 3
    print(f"approval recorded: {p}")
    print("MigrationGuard still does not execute it. Hand the approved file to your "
          "normal migration runner.")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Explain a cassette miss by diffing the built prompt against the nearest recording.

    A miss means the prompt differs from what was recorded, and the prompt is built from
    tool output -- so a miss is almost always a tool leaking something environment-specific
    (a timing, a byte count, a raced observation). This prints the differing lines instead
    of leaving you to guess, which is what the whole of changelog A-8 was.
    """
    import difflib
    import glob

    from .agents import advanced
    from .dataset import load_cases
    from .llm import LLM, CassetteMiss

    case = load_cases([args.case])[0]
    captured: dict[str, str] = {}
    original = LLM.complete

    def spy(self, system, user, agent="agent", tracer=None):
        try:
            return original(self, system, user, agent, tracer)
        except CassetteMiss:
            captured.update(agent=agent, system=system, user=user)
            raise

    LLM.complete = spy  # type: ignore[method-assign]
    try:
        advanced.review(case, llm=LLM(), tracer=Tracer(label=f"diagnose {case.case_id}"))
        print(f"no cassette miss: '{case.case_id}' replays cleanly.")
        return 0
    except CassetteMiss:
        pass
    except Exception as e:
        print(f"'{case.case_id}' failed, but not with a cassette miss: {type(e).__name__}: {e}")
        return 1
    finally:
        LLM.complete = original  # type: ignore[method-assign]

    print(f"cassette miss in the \033[1m{captured['agent']}\033[0m agent on case "
          f"'{case.case_id}'.\n")
    best = None
    for f in glob.glob(str(SETTINGS.cassette_dir / "*.json")):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        if d.get("system") != captured["system"]:
            continue
        ratio = difflib.SequenceMatcher(None, d["user"], captured["user"]).quick_ratio()
        if best is None or ratio > best[0]:
            best = (ratio, f, d["user"])
    if best is None:
        print("no recording exists for this agent at all — record with "
              "MG_LLM_PROVIDER=claude_cli.")
        return 2

    print(f"closest recording: {Path(best[1]).name}  (similarity {best[0]:.3f})")
    print("\n\033[1m- recorded / + built now\033[0m\n")
    diff = list(difflib.unified_diff(best[2].splitlines(), captured["user"].splitlines(),
                                     lineterm="", n=1))
    shown = 0
    for line in diff[2:]:
        if line.startswith(("+", "-")) or shown < 60:
            colour = "\033[32m" if line.startswith("+") else ("\033[31m" if line.startswith("-") else "")
            print(f"  {colour}{line[:200]}\033[0m" if colour else f"  {line[:200]}")
            shown += 1
        if shown > 60:
            print("  ...")
            break
    print("\nEvery differing line is something a tool put in the prompt. If it is a "
          "timing, a\nsize, or an observation that could be raced, that is the bug — make "
          "it categorical.\nIf it is genuinely new content, re-record with "
          "MG_LLM_PROVIDER=claude_cli.")
    return 2


def cmd_doctor(_: argparse.Namespace) -> int:
    import shutil

    from .tools import shadow_db

    print(f"provider         : {SETTINGS.provider}")
    print(f"model            : {SETTINGS.model}")
    print(f"claude cli       : {shutil.which('claude') or 'NOT FOUND'}")
    print(f"cassettes        : {len(list(SETTINGS.cassette_dir.glob('*.json')))} recorded in {SETTINGS.cassette_dir}")
    print(f"psycopg          : {'yes' if shadow_db.HAVE_PSYCOPG else 'NO'}")
    print(f"shadow dsn       : {SETTINGS.shadow_dsn.split('@')[-1]}")
    if shadow_db.HAVE_PSYCOPG and SETTINGS.shadow_enabled:
        p = shadow_db.probe("CREATE TABLE t (id serial PRIMARY KEY, v text);",
                            "ALTER TABLE t ADD COLUMN w text;", SETTINGS)
        print(f"shadow db        : {'OK' if p.get('available') else 'FAILED: ' + str(p.get('reason'))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    SETTINGS.ensure_dirs()
    ap = argparse.ArgumentParser("mg", description="MigrationGuard — review PostgreSQL migrations before they ship")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="review one migration")
    r.add_argument("--schema")
    r.add_argument("--migration")
    r.add_argument("--case", help="use a case id from the bundled dataset instead")
    r.add_argument("--system", default="advanced", choices=["advanced", "baseline", "rules_only"])
    r.add_argument("--json", help="also write the verdict to this path")
    r.set_defaults(func=cmd_review)

    e = sub.add_parser("eval", help="run the benchmark")
    e.add_argument("--systems", default="rules_only,baseline,advanced")
    e.add_argument("--cases", default="")
    e.set_defaults(func=cmd_eval)

    t = sub.add_parser("trajectory", help="print a recorded trajectory")
    t.add_argument("run_id")
    t.set_defaults(func=cmd_trajectory)

    a = sub.add_parser("approve", help="human approval gate (the only path that can touch a real DSN)")
    a.add_argument("--case-id", required=True)
    a.add_argument("--sql-file", required=True)
    a.add_argument("--approved-by")
    a.add_argument("--dsn", required=True)
    a.add_argument("--summary", default="")
    a.set_defaults(func=cmd_approve)

    g = sub.add_parser("diagnose", help="explain why a case misses its cassette")
    g.add_argument("--case", required=True)
    g.set_defaults(func=cmd_diagnose)

    d = sub.add_parser("doctor", help="check the environment")
    d.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
