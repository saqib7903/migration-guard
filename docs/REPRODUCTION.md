# Reproduction guide

Written for someone who has just cloned the repository onto a clean machine and has
never seen it before. The headline result reproduces **offline, with no API key, at zero
cost**, because every model call is replayed from a recorded cassette.

## 0. What you need

| | version used | notes |
|---|---|---|
| Python | 3.11.15 | 3.11+ |
| PostgreSQL | 16.13 | via Docker; any 16.x works. 12+ works with two labels caveated below |
| Docker | 29.4.3 | only to run the throwaway PostgreSQL |
| model | `claude-sonnet-4-5` | **not needed for the headline result** — only to re-record |

Python packages: `psycopg[binary]>=3.1`, `httpx>=0.27`, and `pytest` for the tests. No
other dependencies, no framework.

## 1. Setup (about 2 minutes)

```bash
git clone <this repo> migration-guard && cd migration-guard
python3 -m venv .venv && source .venv/bin/activate   # macOS ships Python 3.9; see note below
pip install -r requirements.txt

cp .env.example .env          # defaults are already the offline/replay settings
make db-up                    # disposable PostgreSQL 16 on localhost:5433
export $(grep -v '^#' .env | xargs)
make doctor                   # should print: shadow db OK, cassettes: 300+
```

`make db-up` starts a container whose data directory is a tmpfs. It holds nothing you
care about; `make db-down` deletes it entirely.

> **Docker Desktop must already be running** — installing it does not start the daemon.
>
> No Docker? Point `MG_SHADOW_DSN` at any throwaway PostgreSQL 16 you can create and drop
> databases on (`brew install postgresql@16 && brew services start postgresql@16` on a
> Mac). Nothing else about the run changes.
>
> **macOS Python:** the system `python3` is 3.9 — too old — and there is no `pip` on
> PATH, only `pip3`. Build the venv from a newer interpreter
> (`brew install python@3.12`, then `/opt/homebrew/bin/python3.12 -m venv .venv`) and run
> `python -m pip install --upgrade pip` inside it before `pip install -e .`; the bundled
> pip 21.2.4 cannot do editable installs from `pyproject.toml`. Extract to a path with no
> spaces. `docs/SETUP_YOUR_ENV.md` has the whole macOS block ready to paste.
>
> The shadow database is required for the `advanced` system — it executes migrations, and
> its cassettes were recorded against real tool output. `mg eval` refuses with
> `PREFLIGHT FAILED` if it is missing, rather than reporting zeros. `--systems
> rules_only,baseline` runs with no database at all.

## 2. The headline result (about 4 minutes, $0.00)

```bash
make eval-replay
```

This runs all three systems over all 29 labelled cases. Model calls come from
`cassettes/`; the shadow database work is executed for real, because it is deterministic
and fast. Expected output:

```
evaluating 29 cases · systems=['rules_only', 'baseline', 'advanced'] · provider=replay
  rules_only  recall=… precision=… decision=… outage_misses=…
  baseline    recall=… precision=… decision=… outage_misses=…
  advanced    recall=… precision=… decision=… outage_misses=…
wrote reports/comparison.md
```

The numbers must match `reports/comparison.md` as committed. They are the numbers quoted
in the README. If they do not match, that is a bug and we want to know.

Artefacts written:

* `reports/comparison.md` — the table in the README, plus per-case decisions and every
  missed risk
* `reports/comparison.json` — the same data, machine-readable, one row per case
* `runs/eval-<system>-<stamp>/<case_id>/trajectory.{jsonl,md}` — the full trajectory of
  every agent on every case

## 3. Watch one review happen

The clearest demonstration is a pair of cases whose SQL differs only in a predicate:

```bash
make review CASE=safe_not_valid_constraint      SYSTEM=rules_only   # safe   (correct)
make review CASE=fails_validate_violating_check SYSTEM=rules_only   # safe   (WRONG)

make review CASE=safe_not_valid_constraint      SYSTEM=baseline     # blocks (WRONG)
make review CASE=fails_validate_violating_check SYSTEM=baseline     # blocks (right label, no evidence)

make review CASE=safe_not_valid_constraint                          # safe   (correct)
make review CASE=fails_validate_violating_check                     # blocks (correct)
```

Both migrations use the idiomatic `ADD CONSTRAINT … NOT VALID; VALIDATE CONSTRAINT;`
pattern. In one, the existing rows satisfy the predicate; in the other they do not, so
`VALIDATE` aborts after the first statement has already committed.

The linter approves both — no rule over the text of these files can separate them. The
single-call baseline blocks both, emitting the same `migration_will_fail` label on the
correct migration as on the broken one: it is pattern-matching the shape, not detecting
the failure. The advanced system gets both right, and on the broken one it prints
PostgreSQL's own `CheckViolation` as the evidence.

Then read what the agent actually did:

```bash
mg trajectory <run_id>        # the run id is printed at the end of every review
```

> If `mg` opens a text editor, it collided with the micro-Emacs binary of the same name.
> Press `Ctrl-X Ctrl-C`, then use `migration-guard trajectory <run_id>` or
> `python -m migration_guard.cli trajectory <run_id>` instead.

## 4. The ablations quoted in the changelog

```bash
make ablations
```

Writes `reports/ablation-verifier-on/` and `reports/ablation-verifier-off/`. Removing the
model entirely and removing the tools entirely are not ablations here — they are the
`rules_only` and `baseline` rows of the headline table, run over all 29 cases.

Note that `MG_SHADOW_ENABLED=0` is **not** a replayable ablation: without the database the
reviewer's prompt changes, so the cassettes miss. That configuration needs re-recording
against a live model. Each switch isolates one component:

| switch | what it removes |
|---|---|
| `MG_ENABLE_VERIFIER=0` | the grounding critic; findings are no longer required to cite evidence |
| `MG_ENABLE_REWRITER=0` + verifier on/off | isolates the verifier exactly; both halves replay from cassettes |
| `--systems rules_only` | the model entirely |

## 5. Tests

```bash
make test        # 24 unit tests, no model, no database, < 1 second
make smoke       # end-to-end with MG_LLM_PROVIDER=stub: no model call at all
```

## 6. Re-recording from live models (optional, costs money)

```bash
export MG_LLM_PROVIDER=claude_cli      # uses an existing `claude` CLI login
# or: export MG_LLM_PROVIDER=anthropic_api ANTHROPIC_API_KEY=sk-ant-...
make eval-live WORKERS=5
```

Approximate cost and runtime for the full 29-case sweep, measured on the recorded run:

| system | model calls | wall clock (5 workers) | cost |
|---|---|---|---|
| `rules_only` | 0 | ~25 s | $0.00 |
| `baseline` | 29 | ~6 min | ~$1.67 |
| `advanced` | ~160 | ~25 min | ~$7.10 |

Cassettes are keyed by prompt hash, so re-recording after a prompt edit only pays for the
prompts that actually changed. Note that model output is not bit-identical across
re-recordings even at temperature 0; the committed cassettes are what make the published
numbers exact.

## 7. Reviewing your own migration

```bash
mg review --schema path/to/schema.sql --migration path/to/0042_add_column.sql
```

`schema.sql` is the schema the migration will land on — `pg_dump --schema-only` of a
staging database is the intended source. Exit code is `0` safe, `1` needs changes,
`2` block, so it drops into CI as a gate.

## 8. Caveats a reviewer should know

* Two labels (`safe_set_not_null_after_check`, `safe_add_column_not_null_default`) depend
  on PostgreSQL 12+ and 11+ fast paths respectively. On an older server those cases are
  genuinely unsafe and the labels would be wrong.
* The shadow database is seeded with 20 000 synthetic rows per table, not your production
  volume. It answers *"does this run, what does it lock, does it rewrite the table"*
  exactly, and *"how long will it take in production"* only as a lower bound.
* The dataset is synthetic and written by us. It contains no customer data, and it is
  also not a random sample of real migrations — it is a deliberately adversarial set.
