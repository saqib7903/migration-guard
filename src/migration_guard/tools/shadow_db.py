"""Shadow database: run the migration for real, in a disposable sandbox, and report facts.

This is the tool that turns opinions into evidence. For each review we

  1. CREATE DATABASE mg_shadow_<random>   (throwaway; dropped in a finally block)
  2. apply the project's base schema and seed it with N synthetic rows
  3. LOCK CAPTURE pass  - run the migration inside BEGIN ... ROLLBACK and, while the
     transaction is still open, read pg_locks for that backend. Locks are held until
     commit, so this captures every relation-level lock the migration really takes,
     even for statements that finish in a millisecond.
  4. EXECUTION pass     - run the migration for real, statement by statement, with a
     statement_timeout, recording duration, rows affected and the exact server error.
  5. schema fingerprint before/after, used to prove a proposed rewrite is equivalent.

Nothing here ever touches a production database: the DSN points at a sandbox cluster and
the only database we write to is one we just created. Applying a migration to anything
real is out of scope for the agent by construction (see agents/approval.py).
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

try:  # psycopg is optional so the static half of the system runs anywhere
    import psycopg
    from psycopg import sql as _sql

    HAVE_PSYCOPG = True
except Exception:  # pragma: no cover
    HAVE_PSYCOPG = False

from ..config import SETTINGS
from .sqlsplit import split_statements

NON_TXN = re.compile(r"\bCONCURRENTLY\b|^\s*VACUUM\b|^\s*CREATE\s+DATABASE\b", re.I)


def _admin_dsn(dsn: str, dbname: str) -> str:
    return re.sub(r"/[^/?]*(\?|$)", f"/{dbname}\\1", dsn)


class ShadowDBError(RuntimeError):
    pass


# --------------------------------------------------------------------------- seeding
_TYPE_EXPR = {
    "integer": "g", "bigint": "g", "smallint": "(g % 30000)",
    "numeric": "(g % 1000)::numeric", "real": "(g % 1000)::real",
    "double precision": "(g % 1000)::double precision",
    "text": "('row_' || g)", "character varying": "('row_' || g)", "character": "('r')",
    "boolean": "(g % 2 = 0)",
    "timestamp without time zone": "now() - (g || ' minutes')::interval",
    "timestamp with time zone": "now() - (g || ' minutes')::interval",
    "date": "(now() - (g || ' days')::interval)::date",
    "uuid": "md5(g::text)::uuid",
    "json": "json_build_object('i', g)", "jsonb": "jsonb_build_object('i', g)",
    "inet": "('10.0.' || (g % 250) || '.1')::inet",
    "bytea": "decode(md5(g::text), 'hex')",
}


def _fk_map(cur) -> dict[tuple[str, str], tuple[str, str]]:
    cur.execute(
        """
        SELECT c.conrelid::regclass::text AS child, a.attname AS col,
               c.confrelid::regclass::text AS parent
        FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'f' AND connamespace = 'public'::regnamespace
        """
    )
    return {(r[0], r[1]): (r[2], "id") for r in cur.fetchall()}


def _table_order(cur) -> list[str]:
    """Parents before children so FK seeds resolve."""
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )
    tables = [r[0] for r in cur.fetchall()]
    cur.execute(
        """
        SELECT c.conrelid::regclass::text, c.confrelid::regclass::text
        FROM pg_constraint c WHERE c.contype='f' AND connamespace='public'::regnamespace
        """
    )
    edges = [(a.strip('"'), b.strip('"')) for a, b in cur.fetchall()]
    ordered: list[str] = []
    remaining = list(tables)
    for _ in range(len(tables) + 1):
        progressed = False
        for t in list(remaining):
            parents = {p for c, p in edges if c == t and p != t and p in remaining}
            if not parents:
                ordered.append(t)
                remaining.remove(t)
                progressed = True
        if not progressed:
            break
    return ordered + remaining


def seed(conn, rows: int) -> dict[str, Any]:
    out: dict[str, Any] = {"seeded": {}, "errors": []}
    with conn.cursor() as cur:
        fks = _fk_map(cur)
        for table in _table_order(cur):
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default, is_identity, udt_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position
                """,
                (table,),
            )
            cols, exprs = [], []
            for name, dtype, nullable, default, identity, udt in cur.fetchall():
                if identity == "YES" or (default or "").startswith("nextval"):
                    continue
                key = (table, name)
                if key in fks:
                    parent = fks[key][0].strip('"')
                    cols.append(name)
                    exprs.append(f"((g - 1) % GREATEST((SELECT count(*) FROM {parent}), 1)) + 1")
                    continue
                expr = _TYPE_EXPR.get(dtype)
                if expr is None:
                    if dtype == "USER-DEFINED":
                        cur.execute(
                            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid "
                            "WHERE t.typname=%s ORDER BY e.enumsortorder LIMIT 1",
                            (udt,),
                        )
                        row = cur.fetchone()
                        if row:
                            expr = f"'{row[0]}'::{udt}"
                    if expr is None:
                        if nullable == "YES" or default:
                            continue
                        out["errors"].append(f"{table}.{name}: no seed expression for {dtype}")
                        continue
                cols.append(name)
                exprs.append(expr)
            # NB: the row count is an int from config and is inlined, so the statement is
            # executed without bound parameters. That keeps `%` usable as the modulo
            # operator inside the generated value expressions.
            n = int(rows)
            if not cols:
                cur.execute(f'INSERT INTO "{table}" SELECT FROM generate_series(1, {n})')
            else:
                collist = ", ".join(f'"{c}"' for c in cols)
                cur.execute(
                    f'INSERT INTO "{table}" ({collist}) SELECT {", ".join(exprs)} '
                    f"FROM generate_series(1, {n}) g"
                )
            out["seeded"][table] = rows
        conn.commit()
        cur.execute("ANALYZE")
        conn.commit()
    return out


# --------------------------------------------------------------------- fingerprinting
def schema_fingerprint(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable, coalesce(column_default,'')
            FROM information_schema.columns WHERE table_schema='public'
            ORDER BY table_name, column_name
            """
        )
        columns = [list(r) for r in cur.fetchall()]
        cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY indexdef")
        indexes = [r[0] for r in cur.fetchall()]
        cur.execute(
            """
            SELECT conrelid::regclass::text || ':' || pg_get_constraintdef(oid)
            FROM pg_constraint WHERE connamespace='public'::regnamespace ORDER BY 1
            """
        )
        constraints = [r[0] for r in cur.fetchall()]
    blob = repr((columns, indexes, constraints)).encode()
    return {
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "hash": hashlib.sha256(blob).hexdigest()[:16],
    }


def relfilenodes(conn) -> dict[str, int]:
    """Physical file id per table.

    A whole-table rewrite (ALTER COLUMN TYPE, VACUUM FULL, a volatile DEFAULT) gives the
    relation a new relfilenode. Comparing this before and after is a *definitive* answer
    to "did PostgreSQL rewrite the table?", which timing alone can never give you.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relname, relfilenode FROM pg_class "
            "WHERE relnamespace='public'::regnamespace AND relkind='r'"
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def table_stats(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT relname,
                   n_live_tup,
                   pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
                   pg_total_relation_size(relid) AS bytes
            FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC
            """
        )
        return [
            {"table": r[0], "live_rows": r[1], "total_size": r[2], "bytes": r[3]} for r in cur.fetchall()
        ]


# ------------------------------------------------------------------------- lock probe
class _LockSampler(threading.Thread):
    def __init__(self, dsn: str, pid: int, interval: float = 0.004):
        super().__init__(daemon=True)
        self.dsn, self.pid, self.interval = dsn, pid, interval
        self.stop_flag = threading.Event()
        self.samples: dict[str, set[str]] = {}
        self.error: str | None = None

    def run(self) -> None:
        try:
            with psycopg.connect(self.dsn, autocommit=True) as conn, conn.cursor() as cur:
                while not self.stop_flag.is_set():
                    cur.execute(
                        """
                        SELECT c.relname, l.mode, l.granted
                        FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
                        WHERE l.pid = %s AND l.locktype = 'relation'
                          AND c.relnamespace = 'public'::regnamespace
                        """,
                        (self.pid,),
                    )
                    for relname, mode, granted in cur.fetchall():
                        self.samples.setdefault(relname, set()).add(
                            mode.replace("Lock", "").upper() + ("" if granted else " (WAITING)")
                        )
                    time.sleep(self.interval)
        except Exception as e:  # pragma: no cover
            self.error = str(e)


_MODE_NAMES = {
    "ACCESSSHARE": "ACCESS SHARE", "ROWSHARE": "ROW SHARE", "ROWEXCLUSIVE": "ROW EXCLUSIVE",
    "SHAREUPDATEEXCLUSIVE": "SHARE UPDATE EXCLUSIVE", "SHARE": "SHARE",
    "SHAREROWEXCLUSIVE": "SHARE ROW EXCLUSIVE", "EXCLUSIVE": "EXCLUSIVE",
    "ACCESSEXCLUSIVE": "ACCESS EXCLUSIVE",
}


def _pretty_mode(m: str) -> str:
    return _MODE_NAMES.get(m.replace(" ", ""), m)


# --------------------------------------------------------------------------- the probe
@contextmanager
def ephemeral_db(dsn: str):
    name = f"mg_shadow_{uuid.uuid4().hex[:10]}"
    admin = _admin_dsn(dsn, "postgres")
    with psycopg.connect(admin, autocommit=True) as c, c.cursor() as cur:
        cur.execute(_sql.SQL("CREATE DATABASE {}").format(_sql.Identifier(name)))
    try:
        yield _admin_dsn(dsn, name), name
    finally:
        with psycopg.connect(admin, autocommit=True) as c, c.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
            )
            cur.execute(_sql.SQL("DROP DATABASE IF EXISTS {}").format(_sql.Identifier(name)))


def probe(base_schema_sql: str, migration_sql: str, settings=SETTINGS) -> dict[str, Any]:
    """Tool entrypoint. Returns observed facts, never opinions."""
    if not HAVE_PSYCOPG:
        return {"available": False, "reason": "psycopg is not installed"}
    if not settings.shadow_enabled:
        return {"available": False, "reason": "shadow DB disabled (MG_SHADOW_ENABLED=0)"}

    t0 = time.time()
    result: dict[str, Any] = {"available": True, "dsn_database": None}
    statements = [s for s in split_statements(migration_sql) if not re.match(r"^\s*(BEGIN|COMMIT|END)\b", s, re.I)]

    try:
        with ephemeral_db(settings.shadow_dsn) as (db_dsn, dbname):
            result["dsn_database"] = dbname
            with psycopg.connect(db_dsn) as setup:
                with setup.cursor() as cur:
                    for s in split_statements(base_schema_sql):
                        cur.execute(s)
                setup.commit()
                result["seed"] = seed(setup, settings.seed_rows)
                result["schema_before"] = schema_fingerprint(setup)
                result["table_stats_before"] = table_stats(setup)
                rfn_before = relfilenodes(setup)

            # ---------------- pass 1: lock capture (rolled back) ----------------
            # Read pg_locks SYNCHRONOUSLY from a second connection after each statement
            # commits inside the open transaction, rather than polling from a sampler
            # thread. A poller races short-lived DDL: a statement that takes ACCESS
            # EXCLUSIVE for one millisecond and then fails releases it before the next
            # poll, so the observed lock set differed between runs -- which changed the
            # prompt, which broke cassette replay on other machines (changelog A-8).
            #
            # Reading at a known point makes the result exact and deterministic: after a
            # successful statement, the locks held are precisely those accumulated so far,
            # because PostgreSQL holds table locks until the transaction ends.
            locks: dict[str, set[str]] = {}
            lock_notes: list[str] = []
            txn_stmts = [s for s in statements if not NON_TXN.search(s)]
            if txn_stmts:
                observer_dsn = _admin_dsn(settings.shadow_dsn, dbname)
                with psycopg.connect(db_dsn) as conn, psycopg.connect(observer_dsn, autocommit=True) as obs:
                    with conn.cursor() as cur:
                        cur.execute(f"SET statement_timeout = {settings.statement_timeout_ms}")
                        cur.execute("SELECT pg_backend_pid()")
                        pid = cur.fetchone()[0]

                    def sample() -> None:
                        with obs.cursor() as ocur:
                            ocur.execute(
                                """
                                SELECT c.relname, l.mode
                                FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
                                WHERE l.pid = %s AND l.locktype = 'relation'
                                  AND l.granted AND c.relnamespace = 'public'::regnamespace
                                """,
                                (pid,),
                            )
                            for relname, mode in ocur.fetchall():
                                locks.setdefault(relname, set()).add(mode)

                    try:
                        with conn.cursor() as cur:
                            for i, stmt in enumerate(txn_stmts):
                                cur.execute(f"SAVEPOINT mg_sp_{i}")
                                try:
                                    cur.execute(stmt)
                                    sample()
                                except Exception as e:
                                    # The subtransaction aborts and its locks are released,
                                    # so there is nothing stable to observe. The error
                                    # itself is the evidence, and pass 2 records it.
                                    lock_notes.append(
                                        f"{re.sub(chr(10), ' ', stmt)[:70]}... -> "
                                        f"{type(e).__name__}: {str(e).strip()[:160]}"
                                    )
                                    cur.execute(f"ROLLBACK TO SAVEPOINT mg_sp_{i}")
                    finally:
                        conn.rollback()
            result["observed_locks"] = {
                rel: sorted({_pretty_mode(m) for m in modes}) for rel, modes in locks.items()
            }
            result["lock_capture_notes"] = lock_notes

            # ---------------- pass 2: real execution ----------------
            exec_rows: list[dict[str, Any]] = []
            failed = False
            with psycopg.connect(db_dsn, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {settings.statement_timeout_ms}")
                for s in statements:
                    st = time.time()
                    row: dict[str, Any] = {"statement": re.sub(r"\s+", " ", s)[:200]}
                    try:
                        cur.execute(s)
                        row["ok"] = True
                        row["rows_affected"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                    except Exception as e:
                        failed = True
                        row["ok"] = False
                        row["error"] = f"{type(e).__name__}: {str(e).strip()[:300]}"
                    row["duration_ms"] = round((time.time() - st) * 1000, 1)
                    exec_rows.append(row)
            result["execution"] = exec_rows
            result["migration_failed"] = failed
            result["total_execution_ms"] = round(sum(r["duration_ms"] for r in exec_rows), 1)

            with psycopg.connect(db_dsn) as conn:
                result["schema_after"] = schema_fingerprint(conn)
                result["table_stats_after"] = table_stats(conn)
                rfn_after = relfilenodes(conn)
                result["rewritten_tables"] = sorted(
                    t for t, n in rfn_after.items() if t in rfn_before and rfn_before[t] != n
                )
    except Exception as e:
        result["available"] = False
        result["reason"] = f"{type(e).__name__}: {e}"

    result["probe_wall_ms"] = round((time.time() - t0) * 1000, 1)
    return result


# Deliberately coarse. Finer buckets flap across runs on sub-millisecond DDL and
# break cassette reproducibility, and they imply a precision this sandbox does not have.
_BUCKETS = [(100, "<100ms"), (1000, "100ms-1s"), (10000, "1-10s")]


def bucket_ms(ms: float | None) -> str:
    """Coarsen a wall-clock measurement before showing it to a model.

    Two reasons, and both matter:

    1. Reproducibility. Prompts are content-addressed for cassette replay, so a raw
       `duration_ms` in a tool result changes the prompt hash on every run and makes the
       benchmark unreproducible. Bucketing makes the agent-facing view deterministic.
    2. Epistemics. Timings on a 20 000-row shadow database with fsync off do not
       transfer to production. A bucket says what it can defend; a number to one decimal
       place invites the model to extrapolate (see changelog A-4).

    Exact timings are kept in the raw probe result and the trajectory, for humans.
    """
    if ms is None:
        return "unknown"
    for limit, label in _BUCKETS:
        if ms < limit:
            return label
    return ">10s"


def _bucket_rank(ms: float | None) -> int:
    order = [b[1] for b in _BUCKETS] + [">10s"]
    return order.index(bucket_ms(ms))


def _coarse(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k != "duration_ms"}
    # For a statement that failed, the error is the finding; how long it took to fail is
    # noise that would only destabilise the prompt hash.
    if row.get("ok"):
        out["duration"] = bucket_ms(row.get("duration_ms"))
    return out


def summarize_for_agent(p: dict[str, Any]) -> dict[str, Any]:
    """Compact, machine-independent view handed to the LLM.

    Everything here is *categorical*: which locks, which errors, whether a table was
    physically rewritten, how many rows. Nothing derived from wall-clock time or from
    on-disk byte counts appears, for two reasons that turn out to be the same reason:

      * Portability. Durations and relation sizes differ between machines, PostgreSQL
        builds and filesystems. Anything time- or size-derived in a prompt makes the
        content-addressed cassettes miss on someone else's laptop, and the published
        benchmark stops reproducing. This was a real defect -- see changelog A-8.
      * Honesty. A 20k-row sandbox with fsync off cannot tell you what a statement costs
        in production, so a number that looks like it can is worse than no number.

    Exact timings and sizes are still recorded in the raw probe result and rendered into
    the trajectory, where a human reads them with the context to discount them.
    """
    if not p.get("available"):
        return {"shadow_db": "unavailable", "reason": p.get("reason")}
    return {
        "shadow_db": "ok",
        "seeded_rows_per_table": dict(sorted((p.get("seed", {}).get("seeded") or {}).items())),
        "observed_locks": dict(sorted((p.get("observed_locks") or {}).items())),
        "tables_physically_rewritten": p.get("rewritten_tables", []),
        "migration_failed": p.get("migration_failed"),
        "errors": [
            {"statement": r["statement"], "error": r["error"]}
            for r in p.get("execution", [])
            if not r.get("ok")
        ],
        "statements_executed": [
            {"statement": r["statement"], "ok": r.get("ok"), "rows_affected": r.get("rows_affected", 0)}
            for r in p.get("execution", [])
        ],
        "schema_hash_before": p.get("schema_before", {}).get("hash"),
        "schema_hash_after": p.get("schema_after", {}).get("hash"),
        "note": "no timings are reported: this sandbox holds 20k synthetic rows with fsync "
                "off, so absolute times would not transfer to production. Judge cost from "
                "the lock taken, whether the table was rewritten, and the row counts.",
    }
