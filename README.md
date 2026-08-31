# MigrationGuard

**An agent that reviews PostgreSQL schema migrations by running them in a disposable
database, and refuses to say anything it did not observe.**

```
$ mg review --schema schema.sql --migration db/migrate/0042_add_currency.sql

● BLOCK  0042_add_currency — VALIDATE CONSTRAINT fails: existing orders violate
                             total_cents > 100000. The deploy aborts half-applied.

  [critical] migration_will_fail
             ALTER TABLE orders VALIDATE CONSTRAINT orders_total_min
             └─ evidence [shadow_db] statement FAILED in shadow run:
                CheckViolation: check constraint "orders_total_min" is violated by some row

  ── proposed rewrite (VALIDATED IN SHADOW DB) ──────────────────────────────
  SET lock_timeout = '3s';
  ALTER TABLE orders ADD CONSTRAINT orders_total_min CHECK (total_cents > 100000) NOT VALID;
```

---

## The user and the bottleneck

**Who.** The backend engineer who wrote `0042_add_currency.sql` on Thursday, and the
staff engineer or DBA who has to approve it before Friday's deploy.

**The bottleneck.** Migration review is the one code review where reading the diff is not
enough, and everybody knows it. `ALTER TABLE orders ALTER COLUMN total_cents TYPE bigint`
is a no-op on a staging table with 400 rows and a fifteen-minute outage on the production
table with 400 million. `ADD CONSTRAINT … NOT VALID` followed by `VALIDATE CONSTRAINT` is
the pattern every safe-migrations guide recommends — and it aborts your deploy if the
predicate does not hold for data nobody has looked at.

So the review depends on facts that are not in the diff: how big is that table, does this
lock block reads or only writes, does PostgreSQL rewrite the file or just touch the
catalogue, and — the one that gets skipped — *does this even run*. Almost nobody checks by
running it, because setting up a realistic database to try it in is a twenty-minute chore
per migration. The result is a review that is part pattern-matching and part hoping, and a
failure mode with a very specific shape: the table is locked, the connection pool fills,
and every service that touches that table falls over together.

**Why solving it is valuable.** This is one of the few remaining places where a routine,
frequent engineering task can take the whole product down, and where the check that would
have caught it is completely mechanical. It is also exactly the wrong job for an LLM
working from text alone: fluent, confident, and structurally uninformed.

---

## What it does

Three tools, three agents, one hard rule.

1. **`static_lint`** — deterministic rules over the SQL. Fast, high recall, some false
   positives. Never hallucinated: every hit points at a literal statement.
2. **`lock_model`** — the lock each statement takes and what that lock blocks, from the
   documented PostgreSQL table-level conflict matrix.
3. **`shadow_probe`** — creates a throwaway database from your schema, seeds it, and
   **runs the migration**. Reports the locks actually held in `pg_locks`, the exact server
   error for anything that fails, and whether `pg_class.relfilenode` changed — a yes/no
   proof of a whole-table rewrite. Everything it reports is categorical, never a timing or
   a byte count, so the same review on a different machine yields the same evidence.

A **reviewer agent** chooses which tools to call and in what order. A **verifier agent**
must cite an id from a deterministically-built evidence catalogue for every finding;
anything it cannot cite is dropped, and a fabricated id resolves to nothing. A **rewriter
agent** proposes a safe version which is then *executed* and schema-diffed against the
original's end state — if it does not run, the server's error goes back to the model and
it tries again, and if it still does not run it is labelled `NOT VALIDATED` rather than
presented as a fix.

Then it stops. MigrationGuard never applies anything, anywhere. `mg approve` requires a
named human and refuses any DSN not explicitly declared disposable.

Architecture and design rationale: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Results

29 labelled migration cases — 21 unsafe, 8 safe. The safe cases are there on purpose: a
reviewer that flags everything gets muted within a week, so crying wolf is scored as a
failure alongside missing an outage. Seven cases do not run at all against their schema
and data; a static analyser cannot reach those by construction.

<!-- RESULTS:BEGIN -->
| Metric | R0 rules only<br/><sub>no model</sub> | B0 baseline<br/><sub>1 call, no tools</sub> | **A advanced**<br/><sub>3 agents, live DB</sub> |
|---|---|---|---|
| **Outage misses** (unsafe called safe) ↓ | 2 | 0 | 0 |
| **Findings backed by tool evidence** ↑ | 38 / 38 | 0 / 48 | 42 / 42 |
| Risk recall ↑ | 0.837 | 0.814 | 0.884 |
| Risk precision ↑ | 0.973 | 0.778 | 0.927 |
| Risk F1 ↑ | 0.9 | 0.795 | 0.905 |
| Decision accuracy ↑ | 0.724 | 0.69 | 0.931 |
| False-alarm risks on the 8 safe cases ↓ | 1 | 4 | 0 |
| Total findings reported | 38 | 48 | 42 |
| Rewrites offered / validated by execution | — | — | 21 / 21 |
| Model calls | 0 | 29 | 160 |
| Cost to record one live sweep | $0.00 | $1.67 | ~$7.10 |
| Cost to reproduce from cassettes | $0.00 | $0.00 | $0.00 |
| Harness errors ↓ | 0 | 0 | 0 |

<sub>Model `claude-sonnet-4-5`, temperature 0, 29 cases (21 unsafe / 8 safe), 20000 seeded rows per table. Generated by `scripts/sync_readme_numbers.py` from `reports/comparison.json` (20260831-004441); ↑ higher is better, ↓ lower is better. Only findings graded `medium` or above count as reported risks, applied identically to every system.</sub>
> **Reproducibility, precisely.** `make eval-replay` serves every model call from
> `cassettes/` and executes all 29 migrations against a live PostgreSQL.
> `scripts/check_reproduction.py` gates it in CI. Verified on two machines: the recording
> box (x86 Linux, PostgreSQL 16.13) reproduces this table exactly on three consecutive
> runs, and an Apple-silicon Mac on PostgreSQL 16.15 reproduces **every metric except
> decision accuracy**, where one of the 29 cases (`safe_create_table`) misses its cassette
> because the two PostgreSQL builds report different lock sets for foreign-key creation.
> `mg diagnose --case safe_create_table` shows the differing lines, and re-recording that
> one case locally closes it. Getting from "reproduces on my laptop" to this took removing
> four sources of hidden non-determinism — changelog A-7 and A-8.

<!-- RESULTS:END -->

Full per-case breakdown, including every missed risk: [`reports/comparison.md`](reports/comparison.md).

**How to read this.** The three systems are close on raw risk-finding and far apart on
everything that decides whether you would put one in front of a deploy:

* **Decision accuracy 0.69 → 0.93.** The baseline finds a risk and then misjudges what to
  do about it, because it does not know whether the migration is a 3 ms catalogue update
  or a table rewrite. That is the number a team feels — the difference between a gate you
  trust and one you click through. 27 of 29 cases correct, against 20 of 29.
* **Precision 0.78 → 0.93, false alarms on safe migrations 4 → 0.** A reviewer that flags
  correct migrations gets muted, and is then worth nothing on the day it is right.
* **Grounded findings 0/48 → 42/42.** Every advanced finding carries an observation a
  tool made. The baseline's are not lies — it never looked at anything, so there is
  nothing to attach, and nothing for a reviewer to check.
* **21 rewrites offered, 21 validated by execution.** The baseline offers none. This is
  the part that gives the engineer their afternoon back.
* **The linter is the most precise system and free to run — and it calls two unsafe
  migrations safe.** Both abort on deploy. It cannot do better: the answer is not in the
  text. That is the gap the shadow database exists to close, and the advanced system
  closes it while also being the highest-recall system on the set.

**Fairness of the comparison.** Same model, same temperature, same risk vocabulary, same
output schema, same cases. The baseline's prompt is a good prompt — it was written to
win, not to lose. The only difference is tools.

**Where the advanced system still loses points.** Its remaining false negatives are
almost all the same behaviour: on a migration the shadow run proves cannot execute, it
reports `migration_will_fail` and stops enumerating the lock profile of statements that
will never run. Defensible, and still counted against it here.

---

## Reproducing this

The headline numbers reproduce **offline, with no API key, at zero cost**: every model
call is content-addressed and recorded in `cassettes/`.

```bash
pip install -r requirements.txt
cp .env.example .env && export $(grep -v '^#' .env | xargs)
make db-up          # disposable PostgreSQL 16 on :5433
make eval-replay    # ~4 minutes, $0.00
```

CI runs exactly this on every push and fails if the reproduced numbers drift from the
published ones (`scripts/check_reproduction.py`).

Full guide, including live re-recording and the ablations:
[`docs/REPRODUCTION.md`](docs/REPRODUCTION.md).
Setting it up against your own database and schema:
[`docs/SETUP_YOUR_ENV.md`](docs/SETUP_YOUR_ENV.md).

---

## Trajectories

Every run writes `runs/<run_id>/trajectory.jsonl`: the agent's instructions, each tool
call, the tool's raw response, every retry with its reason, the verifier's keep/drop
decisions, and the human checkpoint. `mg trajectory <run_id>` renders one.

Representative trajectories for all three agents:
[`docs/TRAJECTORIES.md`](docs/TRAJECTORIES.md).

---

## Improvement changelog

Six iterations, each with the evidence that caused the next — including the experiment I
removed and the two defects the measurements exposed:
[`CHANGELOG_IMPROVEMENTS.md`](CHANGELOG_IMPROVEMENTS.md).

The change that contributed most is the shadow database. The change I am most glad I
reverted is production duration estimation.

---

## Main failure mode

**The shadow database is not your database.** It runs 20 000 synthetic rows on a warm
cache with `fsync=off`. That makes it exact about *what* happens — which locks, which
errors, whether the table is rewritten — and only a lower bound on *how long*. A migration
that takes 30 ms here can take fifteen minutes on your production table, and
MigrationGuard will not tell you that, by design (see changelog A-4). It also seeds
synthetic data, so it can miss a constraint violation that only your real data would
trigger, and it cannot see application code, so `renaming_in_use` and `destructive_drop`
are judged from the schema alone.

The honest framing: this closes the "will it run and what will it lock" gap completely,
and the "how long will it hurt" gap not at all. The second one needs production
statistics, and I would rather ship a tool that is silent about what it cannot measure
than one that is confident about it.

---

## Hot take

The failure mode I keep hitting is not agents that reason badly. It is **agents that
reason fluently about a world they never looked at** — and the tell is that the output
gets *more* persuasive as it gets less connected to reality, because nothing is pushing
back.

Two things fixed it here, and neither was a better prompt. First, give the agent a
disposable copy of reality it can break: the single highest-value tool in this system
creates a database, ruins it, and throws it away. Second, make grounding structural
rather than requested — the verifier cites ids into a catalogue built before any model
saw it, so a fabricated citation resolves to nothing and deletes the claim it was meant
to support. "Cite your sources" is a request. "Your citation must resolve" is a
constraint, and only one of those survives contact with a model that wants to be helpful.

And the unglamorous corollary, which cost me a whole benchmark run: **your evaluation
harness needs error handling before your agent does.** Mine parsed a rate-limit message
as a successful model reply, scored it as "reviewed this migration, found no risks", and
reported a 27-point recall drop as a model quality problem. A crashed agent is visible.
An agent whose tool error got parsed as a confident empty answer is a silent regression
that makes every number after it a lie.

---

## What existed before, and what I built

**Before:** PostgreSQL 16 and its documented lock matrix; the `claude` CLI and the
Anthropic Messages API; `psycopg`; Docker. The safe-migration patterns encoded in the
rewriter (`NOT VALID`/`VALIDATE`, `CONCURRENTLY`, expand-contract) are long-standing
community practice, not my invention.

**Built for this hackathon:** everything in `src/`, `eval/`, `scripts/`, `tests/` and
`docs/` — the three-tool agent pipeline, the shadow-database prober with its lock-capture
and relfilenode techniques, the evidence catalogue and its enforced-citation verifier, the
execution-validated rewriter, the human approval gate, the cassette record/replay layer,
the 29-case labelled dataset, the scoring harness and the trajectory recorder.

**Data:** entirely synthetic, written for this repository. No customer data, no scraped
data, no credentials. `.env` is git-ignored and approval records store the database host
without credentials.

MIT licensed.
