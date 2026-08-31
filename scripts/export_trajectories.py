#!/usr/bin/env python3
"""Collect representative trajectories into docs/TRAJECTORIES.md.

One trajectory per agent role, chosen to show the interesting behaviour:
a tool loop that changes its mind, a verifier that drops an unsupported finding, and a
rewriter that fails validation once and retries with the server's own error message.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from migration_guard.config import SETTINGS  # noqa: E402
from migration_guard.trace import Tracer  # noqa: E402

WANT = [
    ("advanced", "fails_validate_violating_check",
     "Reviewer tool loop: the linter approves the migration, the shadow run refuses it."),
    ("advanced", "safe_widen_varchar",
     "Grounding: relfilenode evidence stops the agent reporting a rewrite that never happened."),
    ("advanced", "multi_risk_release",
     "Full pipeline on a realistic four-problem release migration, including the rewrite."),
    ("baseline", "fails_missing_column",
     "Baseline B0 on the same migration: fluent, confident, and wrong."),
    ("rules_only", "safe_set_not_null_after_check",
     "Ablation R0: the linter alone, with no way to see the validated CHECK constraint."),
]


def newest_run(system: str, case_id: str) -> Path | None:
    hits = sorted(SETTINGS.runs_dir.glob(f"eval-{system}-*/{case_id}"))
    return hits[-1] if hits else None


def main() -> None:
    out = [
        "# Agent trajectories",
        "",
        "Every run MigrationGuard performs writes a `trajectory.jsonl` under `runs/`: the agent's",
        "instructions, each tool call, the tool's raw response, every retry, the verifier's",
        "keep/drop decisions and the human checkpoint. `mg trajectory <run_id>` renders one.",
        "",
        "The trajectories below are copied verbatim from the recorded evaluation runs in",
        "`reports/comparison.json`, so each one can be traced back to a row in the results table.",
        "",
    ]
    for system, case_id, why in WANT:
        run = newest_run(system, case_id)
        out.append(f"## `{system}` · `{case_id}`")
        out.append("")
        out.append(f"*{why}*")
        out.append("")
        if not run:
            out.append("_(no recorded run found — run `make eval-replay` first)_\n")
            continue
        t = Tracer(run_id=str(run.relative_to(SETTINGS.runs_dir)))
        body = t.render_markdown()
        body = "\n".join(body.splitlines()[1:])  # drop its own H1
        out.append(body)
        out.append("\n---\n")

    target = ROOT / "docs" / "TRAJECTORIES.md"
    target.parent.mkdir(exist_ok=True)
    target.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {target} ({target.stat().st_size // 1024} KB)")

    # a compact index of every recorded run, so judges can find any case
    index = []
    for p in sorted(SETTINGS.runs_dir.glob("eval-*/*/trajectory.jsonl")):
        rel = p.parent.relative_to(SETTINGS.runs_dir)
        steps = sum(1 for _ in p.open())
        index.append({"run_id": str(rel), "steps": steps})
    (ROOT / "docs" / "trajectory_index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"indexed {len(index)} runs")


if __name__ == "__main__":
    main()
