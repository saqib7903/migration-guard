# Running MigrationGuard on your own project

Everything MigrationGuard needs is a throwaway PostgreSQL and a way to call a model.
There are no accounts to create, no API besides the model, and nothing to deploy.

`docs/REPRODUCTION.md` covers reproducing the published benchmark. This covers pointing
the tool at a real codebase.

## 1. Install

```bash
git clone https://github.com/saqib7903/migration-guard.git && cd migration-guard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .                       # gives you the `mg` command
```

### macOS, start to finish

The system Python on macOS is 3.9 and this project needs 3.11+, so the venv must be built
from a newer interpreter. Paste this as one block:

```bash
brew install python@3.12 postgresql@16
brew services start postgresql@16

git clone https://github.com/saqib7903/migration-guard.git && cd migration-guard

rm -rf .venv
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

cp .env.example .env
export MG_LLM_PROVIDER=replay
export MG_SHADOW_DSN=postgresql://$USER@127.0.0.1:5432/postgres
export MG_SANDBOX_DSNS=$MG_SHADOW_DSN
mg doctor
```

Three things that will otherwise bite you:

* **Clone to a path with no spaces.** An unquoted path with a space in it breaks `cd`
  and several make targets.
* **Upgrade pip before `pip install -e .`.** The pip that ships with macOS Python is
  21.2.4, which predates PEP 660 and cannot do an editable install from `pyproject.toml`
  alone. A `setup.py` shim is included so it works anyway, but upgrading is cleaner.
* **Homebrew PostgreSQL uses your own username, not `mg`.** `brew install postgresql@16`
  creates a superuser named after your account, so `postgresql://$USER@...` works with no
  `createuser` step at all.

The `mg` command is a convenience, and on some systems it is ambiguous: `mg` is also the
name of the micro-Emacs editor, which ships on several BSD and macOS setups. If typing
`mg doctor` opens a text editor showing `--**-Mg: doctor (fundamental)--L1--`, press
`Ctrl-X Ctrl-C` to leave it and check with `which -a mg`. Two unambiguous alternatives,
both identical:

```bash
migration-guard doctor                 # same entry point, unambiguous name
python -m migration_guard.cli doctor   # no install needed at all
```

Every `make` target calls `python -m migration_guard.cli` directly, so the collision never
affects `make eval-replay`, `make verify-repro`, `make review` or `make ablations`.

## 2. Configuration

Copy `.env.example` to `.env` and edit the four lines that matter. The file is
git-ignored; never put a production DSN in it.

```bash
# --- model ---------------------------------------------------------------
# If the Claude CLI is already logged in, this needs no API key:
MG_LLM_PROVIDER=claude_cli
MG_MODEL=claude-sonnet-4-5
MG_RECORD=1                 # keep this on: it builds the replay cassettes

# If you would rather burn API credits than CLI credits:
# MG_LLM_PROVIDER=anthropic_api
# ANTHROPIC_API_KEY=sk-ant-...

# --- shadow database -----------------------------------------------------
MG_SHADOW_DSN=postgresql://mg:mg@127.0.0.1:5433/postgres
MG_SEED_ROWS=20000

# --- the approval gate ---------------------------------------------------
# `mg approve` refuses any DSN not in this list. Production must never appear here.
MG_SANDBOX_DSNS=postgresql://mg:mg@127.0.0.1:5433/postgres
```

Load it in your shell:

```bash
export $(grep -v '^#' .env | xargs)
```

## 3. The shadow database

You have three options; pick one.

**a. Docker (what the repo assumes, and what the reproduction guide uses)**

```bash
make db-up          # postgres:16-alpine on :5433, tmpfs data dir, dies with `make db-down`
```

Docker **Desktop must be running first** — the daemon is not started by installing it. If
you see `failed to connect to the docker API at unix:///Users/you/.docker/run/docker.sock`,
open Docker Desktop, wait for the whale icon to settle, and re-run.

**b. A local PostgreSQL you already run**

Any PostgreSQL 16 where your user can `CREATE DATABASE` works. MigrationGuard only ever
creates `mg_shadow_<random>` databases and drops them again.

```bash
brew install postgresql@16
brew services start postgresql@16
createuser mg --createdb
export MG_SHADOW_DSN=postgresql://mg@127.0.0.1:5432/postgres
export MG_SANDBOX_DSNS=$MG_SHADOW_DSN
```

This is the better option on a Mac if Docker Desktop is being difficult — it is one brew
install and the results are identical.

**c. No database at all**

```bash
export MG_SHADOW_ENABLED=0
mg eval --systems rules_only,baseline      # these two need no database
```

The `advanced` system cannot run without one — it executes migrations, that is the whole
idea — and its recorded cassettes were made with a live database, so they will not replay
against a different tool result. `mg eval` and `mg review` refuse up front with a
`PREFLIGHT FAILED` message rather than silently reporting 0.0.

Check it:

```bash
mg doctor
```

## 4. Pointing it at your own schema

The one input MigrationGuard needs from your project is the schema the migration will
land on. Get it from staging, never from production:

```bash
pg_dump --schema-only --no-owner --no-privileges "$STAGING_DSN" > schema.sql
mg review --schema schema.sql --migration db/migrate/0042_add_currency.sql
```

Exit codes: `0` safe, `1` needs changes, `2` block. That is the CI gate.

```yaml
# .github/workflows/migrations.yml
- name: MigrationGuard
  run: |
    for f in $(git diff --name-only origin/main... -- 'db/migrate/*.sql'); do
      mg review --schema schema.sql --migration "$f" || exit $?
    done
```

## 5. If you want to apply something

You don't, from here. `mg approve` records a human decision and hands the file back:

```bash
mg approve --case-id 0042_add_currency \
           --sql-file rewrite.sql \
           --approved-by "Your Name <you@example.com>" \
           --dsn "$MG_SHADOW_DSN" \
           --summary "rewrite validated in shadow, batched backfill"
```

Point it at anything not in `MG_SANDBOX_DSNS` and it refuses. That refusal is the feature.

## 6. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `PREFLIGHT FAILED ... not reachable` | no shadow database | start Docker Desktop, then `make db-up` |
| `docker.sock: no such file` | Docker Desktop not running | open Docker Desktop and wait for it to start |
| `zsh: command not found: pip` | venv not active on macOS | `source .venv/bin/activate`, or use `pip3` |
| `advanced recall=0.0` with cassette misses | shadow database was down | fixed by the preflight above; start the DB and re-run |
| `CassetteMiss` | you edited a prompt while in replay mode | re-record with `MG_LLM_PROVIDER=claude_cli`, or `git checkout` the prompt |
| `permission denied to create database` | shadow user lacks CREATEDB | `ALTER ROLE mg CREATEDB;` |
| eval is slow | serial by default | `MG_EVAL_WORKERS=5` |
| `claude cli failed rc=1` | CLI not logged in | run `claude` once interactively |
