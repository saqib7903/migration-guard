#!/usr/bin/env python3
"""Fail if a fresh replay run does not reproduce the committed headline numbers.

This is what makes "reproducible" a test rather than a claim.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = json.loads((ROOT / "reports" / "comparison.json").read_text())
EXPECTED = json.loads((ROOT / "reports" / "published_numbers.json").read_text())

# `errors` is in this list deliberately. A case that crashes or misses its cassette is
# scored as a complete miss, which moves the metrics in ways that look like a quality
# regression -- exactly the confusion that cost an hour when this benchmark was first run
# on someone else's laptop (changelog A-8).
KEYS = ["risk_recall", "risk_precision", "decision_accuracy", "outage_misses",
        "false_alarm_risks_on_safe_cases", "ungrounded_findings", "rewrites_validated",
        "errors"]

bad = []
for system, expected in EXPECTED.items():
    if system not in CURRENT["systems"]:
        bad.append(
            f"{system}: not present in reports/comparison.json — the last eval did not "
            f"include it. Run: mg eval --systems {','.join(EXPECTED)}"
        )
        continue
    got = CURRENT["systems"][system]["summary"]
    for k in KEYS:
        if k in expected and got.get(k) != expected[k]:
            bad.append(f"{system}.{k}: published {expected[k]!r}, reproduced {got.get(k)!r}")

if bad:
    print("REPRODUCTION MISMATCH")
    for b in bad:
        print("  -", b)
    failed = [
        (sys_name, c["case_id"], c["error"])
        for sys_name in CURRENT["systems"]
        for c in CURRENT["systems"][sys_name]["cases"]
        if c.get("error")
    ]
    if failed:
        print("\n  Cases that did not complete (each is scored as a total miss):")
        for sys_name, cid, err in failed:
            print(f"    {sys_name}/{cid}: {err.splitlines()[0][:140]}")
        print("\n  Diagnose a cassette miss with:")
        print(f"    python -m migration_guard.cli diagnose --case {failed[0][1]}")
    sys.exit(1)
print(f"reproduction OK — {len(EXPECTED)} systems match the published numbers")
