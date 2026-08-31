"""The agent-facing view of the shadow database must be byte-identical across runs.

If it is not, the prompt hash changes, every cassette misses, and the published benchmark
stops reproducing. This was a real defect (improvement changelog A-7), so it has a test.

Requires a reachable shadow PostgreSQL; skipped otherwise.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from migration_guard.dataset import load_cases  # noqa: E402
from migration_guard.evidence import build_catalog, render  # noqa: E402
from migration_guard.tools import lock_model, shadow_db, static_rules  # noqa: E402
from migration_guard.tools.sqlsplit import split_statements  # noqa: E402

CASES = ["multi_risk_release", "fails_unique_after_backfill", "drop_table", "safe_widen_varchar"]


def _agent_view(case) -> str:
    p = shadow_db.probe(case.base_schema_sql, case.migration_sql)
    if not p.get("available"):
        pytest.skip(f"shadow db unavailable: {p.get('reason')}")
    lint = static_rules.lint(case.migration_sql)
    locks = lock_model.analyze(split_statements(case.migration_sql))
    return (
        json.dumps(shadow_db.summarize_for_agent(p), sort_keys=True, default=str)
        + "\n"
        + render(build_catalog(lint, locks, p))
    )


@pytest.mark.parametrize("case_id", CASES)
def test_agent_facing_view_is_stable_across_runs(case_id):
    case = load_cases([case_id])[0]
    first = _agent_view(case)
    second = _agent_view(case)
    assert first == second, "shadow-db view changed between identical runs -> cassettes will miss"


def test_durations_are_bucketed_not_raw():
    assert shadow_db.bucket_ms(0.4) == shadow_db.bucket_ms(37.9) == "<100ms"
    assert shadow_db.bucket_ms(450) == "100ms-1s"
    assert shadow_db.bucket_ms(120_000) == ">10s"
    view = shadow_db.summarize_for_agent(
        {
            "available": True,
            "execution": [{"statement": "x", "ok": True, "duration_ms": 3.14159}],
            "total_execution_ms": 3.14159,
        }
    )
    assert "3.14" not in json.dumps(view)
