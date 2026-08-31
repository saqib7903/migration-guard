# Improvement changelog

Each entry is a change, the evidence that motivated it, and the evidence it produced —
which is what motivated the next one. Every number here comes from a file in `reports/`
or `runs/` that ships with this repository; nothing is quoted from memory.

Systems: **R0** rules only (no model) · **B0** baseline (one model call, no tools) ·
**A** the advanced pipeline.

---

## B0 — the fair baseline

**Built first, deliberately.** One model call: the migration, the target schema, the same
risk vocabulary and the same JSON contract the advanced system uses. Temperature 0, same
model. Nothing about the comparison is rigged by giving the baseline a worse prompt — the
only thing it lacks is tools.

*Evidence produced:* B0 is fluent and eager. It reports far more risks than exist
(precision ≈ 0.53 in the first recorded sweep, `reports/history/v1-before-fixes.json`)
and it approves migrations that cannot run. Both failure modes matter, and they point in
opposite directions, so "just tune the prompt" was not going to fix it.

---

## R0 — the ablation nobody wants to run

**Change.** Before adding any agent machinery, wrote the deterministic linter as a
standalone system and scored it. If a linter is enough, an LLM is a liability.

*Evidence produced.* R0 scores **recall 0.837 / precision 0.973** on the 29-case set. It
is *better than the single-call LLM at not crying wolf*, and it is free. But it has a hard
ceiling: it scores exactly **0 of the 7 cases labelled `migration_will_fail`**, because
"does this SQL run against this schema and this data" is not a property of the text.

*Decision.* Keep R0 permanently as a system, not a footnote — it sets the bar the agent
has to clear, and it is the honest answer to "did you need an LLM for this?". The gap it
cannot close became the design target.

---

## A-1 — the shadow database *(the change that contributed most)*

**Change.** Gave the reviewer agent a tool that creates a throwaway PostgreSQL database
from the project's schema, seeds it with synthetic rows, and **runs the migration**. It
reports the locks observed in `pg_locks`, per-statement duration, and the server's exact
error for anything that fails.

*Why this and not more reasoning.* Every failure R0 could not reach had the same shape:
the answer lived in the database, not in the file. No amount of prompt engineering
retrieves a fact that is not in the prompt.

*Evidence produced.* On `fails_validate_violating_check` — an idiomatic
`ADD CONSTRAINT … NOT VALID` followed by `VALIDATE CONSTRAINT` — R0 and B0 both approve
it. The shadow run returns:

```
CheckViolation: check constraint "orders_total_min" of relation "orders" is violated by some row
```

The agent's verdict flips from *safe* to *block*, and the rationale it prints is the
server's own message. Trajectory: `docs/TRAJECTORIES.md`, first entry.

*Design detail worth stealing.* Locks are read from a second connection **while the
migration's transaction is still open**, with a savepoint per statement, synchronously
after each statement succeeds. PostgreSQL holds table locks until the transaction ends, so
at that instant the locks held are exactly those accumulated so far — which catches DDL
that finishes in a millisecond, without the race a polling sampler has (see A-8).

---

## A-2 — the verifier, and grounding as a mechanism

**Change.** Added a second agent that sees only the findings and an **evidence catalogue**
— every tool observation, assigned a stable id (`E1`, `E2`, …) deterministically by
`evidence.py` before any model sees it. The verifier must cite ids per finding.
`resolve()` keeps only ids that exist in the catalogue, so a fabricated citation resolves
to nothing and the finding it was meant to support is dropped for lack of support.

*Why not just ask the reviewer to be careful.* Because "cite your evidence" is a request,
and requests are satisfied by plausible-looking text. Making the citation resolve against
a real table turns it into a constraint.

*Evidence produced.* `ungrounded_findings` is **0 of 42** for the advanced system against
**48 of 48** for B0 — the baseline has nothing to point at, not because it is lying but
because it never looked. False-alarm risks on the 8 cases labelled safe fall from 4 to 0.
See the measured ablation at the end of this file for what the verifier does and does not
buy.

---

## A-3 — the rewriter, validated by execution

**Change.** A third agent proposes a safe rewrite. The rewrite is then **executed** in a
fresh shadow database and schema-diffed (column set, index set) against the end state the
original migration produced. A rewrite that errors is sent back to the model with the
server's error text attached, once. A rewrite that still fails is labelled
`NOT VALIDATED — do not trust` rather than displayed as a fix.

*Evidence produced.* **21 of 21** rewrites offered in the final sweep passed execution +
schema equivalence. The retry loop earns its place: rewrites that used
`CONCURRENTLY` inside a `BEGIN` block failed the first probe and were corrected from the
error message (visible as `retry` steps in the trajectories).

*Why it matters more than the finding.* A reviewer that says "this is unsafe" costs an
engineer an afternoon. A reviewer that hands over a rewrite it has already run costs
them ten minutes — but only if the rewrite is real, which is why an unvalidated one is
labelled loudly instead of quietly.

---

## A-4 — production duration estimates *(removed)*

**Change.** Had the agent multiply shadow-database timings by the ratio of production row
count to seeded row count and report "this will lock `orders` for ~4 minutes".

*Evidence that killed it.* The shadow database runs 20 000 rows on a warm page cache with
`fsync=off`. The extrapolation was off by more than an order of magnitude in both
directions depending on the statement, and it was the most confident-sounding line in the
whole report. Reviewers anchor on numbers; a wrong number is worse than no number.

*Replaced with.* `pg_class.relfilenode` before and after. It answers a *smaller* question
— "did PostgreSQL physically rewrite this table, yes or no" — **exactly**, instead of
answering a bigger one badly. This is the trade I would make again every time.

*Evidence produced.* On `safe_widen_varchar` (`varchar(50)` → `varchar(200)`), the
catalogue entry reads *"no relfilenode changed → no table was physically rewritten"*, and
the agent stops reporting the rewrite that reviewers habitually claim there.

---

## A-5 — collapsing an overlapping label in the risk vocabulary

**Change.** Removed `access_exclusive_lock` from the taxonomy.

*Evidence that motivated it.* In the first full sweep
(`reports/history/v1-before-fixes.json`) the advanced system emitted
`access_exclusive_lock` as a false positive on **5 cases**, and on
`drop_index_blocking` and `multi_risk_release` it emitted it *instead of* the specific
label (`blocking_index_build`), costing recall. Reading the trajectories made the cause
obvious: taking ACCESS EXCLUSIVE is the *mechanism* behind almost every other label, so
it was always a defensible answer, and a label that is always defensible is a label the
model will always reach for.

*Change made.* Dropped the label; added one line to the output contract telling the agent
to pick the most specific label that applies.

*Lesson.* A risk taxonomy is part of the system under test. An overlapping label is a
silent recall bug that looks like a model quality problem.

---

## A-6 — API errors were being scored as "found nothing"

**Change.** The `claude` CLI exits `0` on rate limits and transport failures and puts the
message in its `result` field. The harness parsed that as a model answer, producing an
empty verdict, which the scorer recorded as *the system reviewed this migration and found
no risks*.

*Evidence that caught it.* **10 of 29 advanced cases** in the first sweep came back with
zero findings and an `LLMError`/empty result, dragging measured recall from a real number
down to 0.61. The failure was in the *measurement rig*, not the system, which is exactly
the kind of thing that quietly invalidates a benchmark.

*Change made.* Detect `is_error`, retry with backoff, and raise loudly if it persists.
Error cases are counted in the report as `errors` and scored as complete misses, never
silently skipped. Also dropped evaluation concurrency from 5 workers to 3.

*Lesson, and the one I would tell anyone shipping agents:* your evaluation harness needs
error handling more urgently than your agent does. A crashed agent is visible. An agent
whose tool returned an error string that got parsed as a successful empty answer is a
silent regression that makes every subsequent number a lie.

---

## A-7 — the benchmark was not actually reproducible, and the fix improved the product

**Evidence that caught it.** Replaying the recorded run reported `CassetteMiss` on 29 of
29 advanced cases. Cassettes are keyed by `sha256(model + system + user)`, so a miss means
the prompt was different — and the prompt is built from tool output. The culprit was
`duration_ms: 3.1` in the shadow-database summary. Wall-clock timings differ on every run,
so the "deterministic" benchmark was silently generating a new prompt every time.
`observed_locks` dict ordering did the same thing more rarely.

**Change made.** The agent-facing view of the shadow database is now coarsened:
durations bucket to `<100ms` / `100ms-1s` / `1-10s` / `>10s`, failed statements report no
duration at all (the error is the finding), and every dict handed to a model is
key-sorted. Exact timings stay in the raw probe result and the trajectory, where a human
reads them. `tests/test_determinism.py` runs four cases twice and asserts the views are
byte-identical.

**Why this made the product better, not just the benchmark.** Bucketing is the same
argument as A-4 one level down: this sandbox has 20 000 rows and `fsync=off`, so
`3.1 ms` is a *true* measurement of something that does not transfer, and handing it to a
model invites exactly the extrapolation A-4 was removed for. The bucket is the honest
resolution of the instrument.

*Lesson.* Cassette replay is not just a cost optimisation. Because it is
content-addressed, it works as a **detector for hidden non-determinism in your tool
outputs** — the miss rate told me my tools were leaking entropy into prompts before any
metric moved. I would put this in every agent system I build.

---

## Where the improvement actually comes from

| component removed | what happens |
|---|---|
| the shadow database (`MG_SHADOW_ENABLED=0`) | the 7 `migration_will_fail` cases become unreachable; the system degrades to roughly R0 quality with an LLM's variance on top |
| the verifier (`MG_ENABLE_VERIFIER=0`) | findings stop carrying evidence; false alarms on safe cases return |
| the model (`--systems rules_only`) | recall 0.837, precision 0.973, and a hard zero on every execution-dependent case |

Reproduce all three with `make ablations`.

---

## A-8 — the benchmark reproduced on my machine and nowhere else

**Evidence that caught it.** Someone ran `make eval-replay` on an Apple-silicon Mac with
Homebrew PostgreSQL 16.15, against cassettes recorded on x86 PostgreSQL 16.13.
`rules_only` and `baseline` matched to three decimal places. `advanced` came back
`recall 0.721` against a published `0.884`, with cassette misses across the set.

Nothing was wrong with the agent. Three machine-dependent values were reaching the
prompts, and the prompt is what the cassette is keyed on:

1. **Wall-clock duration buckets.** A 20 000-row `UPDATE` took `100ms-1s` on the recording
   box and `<100ms` on the faster laptop. The bucket boundary ran straight through the
   most common statement in the set.
2. **`pg_total_relation_size`.** The evidence catalogue quoted `2128 kB`, which differs
   between builds and filesystems.
3. **A polling lock sampler.** Locks were read by a background thread every 4 ms. A
   statement that takes ACCESS EXCLUSIVE for one millisecond and then *fails* releases it
   before the next poll, so the observed lock set was a coin flip — non-deterministic even
   on one machine, which is why replays here had been drifting by about one case.

**Changes made.** The agent-facing view of the shadow database is now entirely
*categorical*: which locks, which errors, whether a table was physically rewritten, how
many rows. No durations, no byte counts. And the lock sampler is gone — `pg_locks` is now
read **synchronously from a second connection after each statement succeeds**, at a point
where the answer is exact rather than raced: PostgreSQL holds table locks until the
transaction ends, so the locks held at that moment are precisely those accumulated so far.

**Evidence produced.** Four consecutive probes of all 29 cases now produce byte-identical
tool output and evidence catalogues, three consecutive full replays produce identical
metrics, and the numbers reproduce on the Mac that found the bug.

*Why this made the product better, not just the benchmark.* It is A-4's argument taken one
level further down. A duration measured on 20 000 synthetic rows with `fsync=off` is a
true number about something that does not transfer, and putting it in front of a model
invites exactly the extrapolation A-4 was deleted for. Removing it cost nothing the
reviewer was actually using: the lock taken, the error raised and the relfilenode diff
already answer the question.

**What is still open, stated plainly.** One case, `safe_create_table`, still misses on the
second machine: `CREATE TABLE ... REFERENCES orders(id)` reports a different set of locks
on the referenced table under PostgreSQL 16.15 than under 16.13. Everything else — recall,
precision, outage misses, grounded findings, validated rewrites, and both other systems —
matches exactly. Two things came out of that rather than a silent patch:

* `scripts/check_reproduction.py` now compares the **error count** as well as the metrics,
  and prints which case failed. Without that, one case failing to complete showed up as a
  3-point decision-accuracy drop, which reads like a model regression and sent me looking
  in the wrong place for an hour.
* `mg diagnose --case <name>` replays a case, catches the miss, finds the nearest
  recording and prints the differing prompt lines. Every differing line is something a
  tool put in the prompt, so the diff *is* the bug report.

A third defect surfaced on the same machine: the older `claude` CLI there rejected
`--restricted`, and a fixed flag list turned that into
`claude cli failed after 4 attempts: error: unknown option '--restricted'`. Supported
flags are now probed from `claude --help`. They never affect the prompt, so cassette keys
are unchanged.

*Lesson.* **Cross-machine replay is a much sharper instrument than same-machine replay.**
Everything here had been green on my laptop for two days. One run on somebody else's
hardware found three defects in an hour, one of which was a genuine race in the tool. If
your agent has a content-addressed cache, run the reproduction somewhere else before you
publish the number — it is the cheapest non-determinism detector you will ever build.

---

## Ablation results (measured, not asserted)

`reports/ablation-verifier-on/` vs `reports/ablation-verifier-off/`, both replayed from
cassettes with the rewriter disabled so the only difference is the verifier:

| | verifier on | verifier off |
|---|---|---|
| risk precision | 0.905 | 0.905 |
| risk recall | 0.884 | 0.884 |
| findings with tool evidence | **42 / 42** | **0 / 42** |

**This is not the result I expected, and it is the more useful one.** On this dataset the
verifier drops nothing — the reviewer's findings were all supportable, because it had
already read the tool output that supports them. What the verifier actually buys is
*auditability*: with it off, the same 42 findings are printed with nothing a human can
check them against. So the honest claim is that the verifier is an evidence-binding
mechanism, not an accuracy mechanism, on a 29-case set where the reviewer happened to
behave. I would expect it to start dropping findings on a larger and messier corpus, and
I have no evidence for that yet, so it is not claimed.

The other two ablations are the `rules_only` and `baseline` rows of the headline table
themselves: removing the model entirely, and removing the tools entirely. Both are full
29-case systems rather than footnotes.
