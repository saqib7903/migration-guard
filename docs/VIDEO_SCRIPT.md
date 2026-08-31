# 5-minute solution video — shot list and script

Every command below is real and runs in this repo. Numbers are the published ones from
`reports/comparison.json`. Target 4:40, leaving slack.

**Setup before you hit record:** `make db-up && mg doctor` (shadow db OK), clear terminal
history, `.env` not on screen, terminal at ~16pt, 1920×1080.

---

## The spine of the video: two migrations that differ by one predicate

```sql
-- eval/dataset/cases/safe_not_valid_constraint/migration.sql
SET lock_timeout = '3s';
ALTER TABLE orders ADD CONSTRAINT orders_total_nonneg CHECK (total_cents >= 0) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_total_nonneg;

-- eval/dataset/cases/fails_validate_violating_check/migration.sql
SET lock_timeout = '3s';
ALTER TABLE orders ADD CONSTRAINT orders_total_min  CHECK (total_cents > 100000) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_total_min;
```

Same shape. Same textbook-correct pattern. One ships fine, one aborts your deploy
half-applied. What separates them is not in either file — it is in the data.

| | safe one | broken one |
|---|---|---|
| the linter | safe ✅ | **safe ❌** |
| one LLM call | **needs_changes ❌** | block ✅ |
| MigrationGuard | safe ✅ | block ✅ |

The detail to land hard: the baseline emits the label `migration_will_fail` on **both**
files. It is not detecting anything. It is pattern-matching "VALIDATE looks scary."

---

## 0:00–0:40 · The problem

*Both migration files open side by side in an editor.*

> These two migrations are the same migration. Add a check constraint `NOT VALID` so it
> doesn't scan the table, then validate it separately — the pattern every safe-migrations
> guide recommends. The only difference is the number in the predicate.
>
> One of them ships. The other takes your deploy down: `VALIDATE` fails on rows that
> already exist, and by then the first statement has committed. You have a half-applied
> migration and a rollback to write at 6pm on a Friday.
>
> You cannot see which is which by reading them. It isn't a property of the SQL. It's a
> property of the data. That's the real problem with migration review: it's a question
> about state, and we answer it by reading text.

## 0:40–1:25 · Both baselines fail, in opposite directions

*Run all four, quickly — two commands per system:*

```bash
make review CASE=fails_validate_violating_check SYSTEM=rules_only
make review CASE=safe_not_valid_constraint      SYSTEM=rules_only
make review CASE=safe_not_valid_constraint      SYSTEM=baseline
make review CASE=fails_validate_violating_check SYSTEM=baseline
```

> The linter — no model at all, 40 deterministic rules. It approves both. It's
> *structurally* incapable of doing better: there is no rule you can write over the text
> of these files that separates them.
>
> Now the baseline: one model call, same model, same temperature, same risk vocabulary,
> same output schema as the real system. The only thing it lacks is tools.
>
> It blocks the *safe* one. And on the broken one — look at the label. `migration_will_fail`.
> The same label it just put on the good migration. It didn't detect the failure. It
> pattern-matched "VALIDATE looks risky" and got lucky on one of two.
>
> That's the failure mode I actually care about. Not a model that's wrong — a model
> that's *unfalsifiable*. There's nothing under that claim you can check.

## 1:25–2:50 · One real execution, end to end

```bash
make review CASE=fails_validate_violating_check
```

*Let it run. Narrate over the tool calls appearing.*

> The reviewer agent has three tools and picks its own order. Linter first — cheap
> candidates. Then the lock matrix: which lock each statement takes and what that lock
> blocks, straight out of the PostgreSQL docs.
>
> Then the one that matters. `shadow_probe` creates a throwaway database, applies your
> schema, seeds twenty thousand rows, and **runs the migration**.
>
> Two techniques in there worth a sentence each. It holds the transaction open and reads
> `pg_locks` from a second connection — locks are held until commit, so it catches every
> lock even on DDL that finishes in a millisecond. And it diffs `pg_class.relfilenode`
> before and after, which is a yes-or-no proof of whether PostgreSQL rewrote the table.
> Timing can never answer that.

*Point at the tool result on screen:*

```
CheckViolation: check constraint "orders_total_min" of relation "orders"
                is violated by some row
```

> That's not the agent's opinion. That's PostgreSQL refusing.
>
> Then two more agents. The verifier gets an evidence catalogue — every observation with
> an id — and has to cite ids per finding. Anything it can't cite gets dropped. And a
> made-up id resolves to nothing, so a fabricated citation deletes the finding it was
> meant to save. Grounding is enforced by the data flow, not requested in a prompt.
>
> Then the rewriter proposes a fix — and the fix is *executed* in a fresh shadow database
> and schema-diffed against the original's end state. If it doesn't run, the server's own
> error goes back to the model and it tries again. This one is labelled "validated in
> shadow DB" because it actually ran. 21 of 21 rewrites in the benchmark passed that gate.
>
> Then it stops. It never applies anything. `mg approve` needs a named human and refuses
> any DSN not on the sandbox list.

*Optional 5 seconds:* `mg trajectory <run_id> | head -60` — scroll it, don't read it.
Just show the raw record exists.

## 2:50–3:40 · The comparison

*On screen:* `reports/comparison.md`

> 29 labelled cases. 21 unsafe, 8 safe. The safe ones are there on purpose — a reviewer
> that flags correct migrations gets muted in a week, and is then worth nothing on the
> day it's right.

Read these four rows and stop:

| | rules-only | baseline | **advanced** |
|---|---|---|---|
| Decision accuracy | 0.724 | 0.690 | **0.966** |
| Risk precision | 0.973 | 0.778 | **0.905** |
| Findings backed by tool evidence | 38/38 | **0/48** | **42/42** |
| Migrations that don't run, caught | **0/7** | 5/7 | **7/7** |

> 28 of 29 decisions right, against 20 of 29. Seven cases in this set don't execute at
> all — the linter gets zero of them by construction, the baseline guesses five, and the
> agent gets all seven because it ran them.
>
> And the row I'd point a judge at: zero of the baseline's 48 findings carry a tool
> observation. Not because it's lying — it never looked at anything, so there's nothing
> to attach and nothing for a reviewer to check.
>
> The linter is still the most precise system here, and free. And it calls two unsafe
> migrations safe. That's the honest bar this had to clear.

## 3:40–4:20 · The changelog: what mattered, what I cut

*On screen:* `CHANGELOG_IMPROVEMENTS.md`

> Seven iterations, each with the evidence that caused the next.
>
> **The change that contributed most** is the shadow database — A-1. It's the only thing
> in the system that can answer "does this run", which is the entire seven-case gap.
>
> **The experiment I removed** is A-4. I had the agent extrapolate shadow timings to
> production row counts and print "this locks `orders` for about four minutes." It was
> the most confident-sounding line in the report and it was off by an order of magnitude
> in both directions. I cut it and replaced it with the relfilenode check — a smaller
> question answered exactly, instead of a bigger one answered badly.
>
> **And the ablation that didn't go how I expected:** turning the verifier off changes
> precision and recall by nothing at all. Zero. What it changes is 42 grounded findings
> to 42 ungrounded ones. So the honest claim is that the verifier buys auditability, not
> accuracy, on this set — and that's what the changelog says, instead of the win I was
> hoping to report.

## 4:20–4:40 · Hot take

> The failure mode I keep hitting isn't agents that reason badly. It's agents that reason
> *fluently about a world they never looked at* — and the tell is that the output gets
> more persuasive as it gets less connected to reality, because nothing is pushing back.
> That baseline blocking a correct migration with a confident label is exactly what it
> looks like.
>
> Two things fixed it, and neither was a better prompt. Give the agent a disposable copy
> of reality it's allowed to break. And make grounding structural instead of requested —
> "cite your sources" is a request, "your citation must resolve" is a constraint, and
> only one of those survives contact with a model that wants to be helpful.
>
> A migration reviewer that can't run migrations is a very expensive spell-checker.

---

## If you have 20 spare seconds

The best one to spend them on is the reproducibility beat, right after the comparison
table — it's 15% of the score and nobody else will show it:

```bash
make eval-replay && make verify-repro
```

> Every model call is content-addressed and recorded. This reruns the whole benchmark
> offline, with no API key, for nothing, and CI fails if the numbers drift.

## Do not record

- Building or explaining the codebase file by file
- The dataset generator
- Anything about the tooling you used to write it
- A UI. There isn't one, and that's the right call.
