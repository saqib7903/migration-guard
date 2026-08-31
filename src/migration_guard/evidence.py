"""Evidence catalogue.

Every observation a tool produced is given a stable id (E1, E2, ...). Agents may only
cite ids from this catalogue; anything else is discarded by `resolve()`. That is what
makes "grounded" a mechanical property of the pipeline rather than a promise in a prompt.
"""
from __future__ import annotations

from typing import Any

from .models import Evidence



def build_catalog(lint: dict[str, Any], locks: dict[str, Any], shadow: dict[str, Any]) -> dict[str, Evidence]:
    cat: dict[str, Evidence] = {}
    n = 0

    def add(source: str, detail: str, ref: dict[str, Any]) -> None:
        nonlocal n
        n += 1
        cat[f"E{n}"] = Evidence(source=source, detail=detail, ref=ref)

    for f in lint.get("findings", []):
        add("static_lint", f"rule {f['rule_id']} matched: {f['statement']}", {"risk": f["risk"]})
    if lint:
        add(
            "static_lint",
            f"migration has {lint.get('statement_count', 0)} statements; "
            f"explicit transaction={lint.get('in_explicit_transaction')}; "
            f"lock_timeout set={lint.get('has_lock_timeout')}",
            {},
        )

    for row in locks.get("per_statement", []):
        blocked = [k for k, v in row["blocks"].items() if v]
        add(
            "lock_model",
            f"{row['statement']} -> {row['lock_mode']}; blocks {blocked or ['nothing']}",
            {"lock_mode": row["lock_mode"], "blocks": blocked},
        )

    if shadow.get("available"):
        for rel, modes in sorted((shadow.get("observed_locks") or {}).items()):
            add("shadow_db", f"observed lock(s) on '{rel}' in a live PostgreSQL run: {', '.join(modes)}",
                {"relation": rel, "modes": modes})
        rewritten = shadow.get("rewritten_tables") or []
        if rewritten:
            add("shadow_db",
                f"relfilenode changed for {', '.join(rewritten)} -> PostgreSQL physically "
                "rewrote the whole table during this migration",
                {"tables": rewritten})
        else:
            add("shadow_db",
                "no relfilenode changed -> no table was physically rewritten by this migration",
                {"tables": []})
        for row in shadow.get("execution", []):
            if not row.get("ok"):
                add("shadow_db", f"statement FAILED in shadow run: {row['statement']} -> {row['error']}",
                    {"error": row["error"]})
            elif row.get("rows_affected"):
                add("shadow_db",
                    f"statement rewrote {row['rows_affected']} rows in the shadow run: {row['statement']}",
                    {"rows_affected": row["rows_affected"]})
        # NB: row counts only, never on-disk size. `pg_total_relation_size` differs between
        # PostgreSQL builds and filesystems, and a catalogue entry that differs by machine
        # changes the prompt hash and breaks cassette replay for everyone else (A-8).
        for st in sorted(shadow.get("table_stats_before", []), key=lambda r: r["table"])[:4]:
            add("shadow_db", f"table {st['table']} holds {st['live_rows']} rows in the shadow run",
                {"table": st["table"], "rows": st["live_rows"]})
    return cat


def render(cat: dict[str, Evidence]) -> str:
    return "\n".join(f"{k}: [{v.source}] {v.detail}" for k, v in cat.items())


def resolve(ids: list[str], cat: dict[str, Evidence]) -> list[Evidence]:
    """Keep only ids that actually exist. Fabricated citations vanish here."""
    return [cat[i] for i in ids if i in cat]
