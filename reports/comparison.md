# MigrationGuard — baseline vs advanced

- generated: `20260831-084426`
- model: `claude-sonnet-4-5` · provider: `replay` · shadow rows/table: `20000`
- cases: **29** (21 labelled unsafe, 8 labelled safe)

| Metric | rules_only | baseline | advanced |
|---|---|---|---|
| Risk recall | 0.837 | 0.814 | 0.884 |
| Risk precision | 0.973 | 0.778 | 0.927 |
| Risk F1 | 0.9 | 0.795 | 0.905 |
| Decision accuracy | 0.724 | 0.69 | 0.931 |
| Outage misses (unsafe called safe) | 2 | 0 | 0 |
| False-alarm risks on safe cases | 1 | 4 | 0 |
| Findings with no tool evidence | 0 | 48 | 0 |
| Rewrites offered | 0 | 0 | 21 |
| Rewrites validated by execution | 0 | 0 | 21 |
| LLM calls | 0 | 29 | 160 |
| Wall seconds | 0.0 | 0.0 | 34.1 |
| Estimated cost (USD) | 0.0 | 0.0 | 0.0 |
| Harness errors | 0 | 0 | 0 |

## Per-case decisions

| Case | expected | rules_only | baseline | advanced |
|---|---|---|---|---|
| `add_column_not_null` | block | ❌ needs_changes | ✅ block | ✅ block |
| `add_unique_constraint` | needs_changes | ✅ needs_changes | ❌ block | ✅ needs_changes |
| `blocking_index` | needs_changes | ✅ needs_changes | ❌ block | ✅ needs_changes |
| `concurrently_in_txn` | block | ✅ block | ✅ block | ✅ block |
| `drop_column` | block | ✅ block | ✅ block | ✅ block |
| `drop_index_blocking` | needs_changes | ✅ needs_changes | ❌ block | ✅ needs_changes |
| `drop_table` | block | ✅ block | ✅ block | ✅ block |
| `fails_drop_column_dependent_view` | block | ✅ block | ✅ block | ✅ block |
| `fails_missing_column` | block | ❌ safe | ✅ block | ✅ block |
| `fails_typo_syntax` | block | ❌ needs_changes | ✅ block | ✅ block |
| `fails_unique_after_backfill` | block | ❌ needs_changes | ✅ block | ✅ block |
| `fails_validate_violating_check` | block | ❌ safe | ✅ block | ✅ block |
| `lock_timeout_missing_only` | needs_changes | ✅ needs_changes | ✅ needs_changes | ✅ needs_changes |
| `multi_risk_release` | block | ✅ block | ✅ block | ✅ block |
| `rename_column` | block | ❌ needs_changes | ✅ block | ❌ needs_changes |
| `safe_add_column_default` | safe | ✅ safe | ✅ safe | ✅ safe |
| `safe_add_column_not_null_default` | safe | ✅ safe | ✅ safe | ✅ safe |
| `safe_concurrent_index` | safe | ✅ safe | ❌ block | ✅ safe |
| `safe_create_table` | safe | ✅ safe | ❌ needs_changes | ✅ safe |
| `safe_drop_index_concurrently` | safe | ✅ safe | ❌ block | ✅ safe |
| `safe_not_valid_constraint` | safe | ✅ safe | ❌ needs_changes | ✅ safe |
| `safe_set_not_null_after_check` | safe | ❌ needs_changes | ✅ safe | ✅ safe |
| `safe_widen_varchar` | safe | ✅ safe | ✅ safe | ✅ safe |
| `set_not_null_scan` | block | ❌ needs_changes | ✅ block | ✅ block |
| `truncate_table` | block | ✅ block | ✅ block | ✅ block |
| `type_change_rewrite` | block | ✅ block | ✅ block | ✅ block |
| `unbounded_backfill` | needs_changes | ✅ needs_changes | ❌ block | ✅ needs_changes |
| `validating_fk` | needs_changes | ✅ needs_changes | ✅ needs_changes | ✅ needs_changes |
| `volatile_default` | needs_changes | ✅ needs_changes | ❌ block | ❌ block |

## Missed risks (false negatives) per system

**rules_only** — 7 missed risk(s)
- `add_column_not_null`: migration_will_fail
- `fails_drop_column_dependent_view`: migration_will_fail
- `fails_missing_column`: migration_will_fail
- `fails_typo_syntax`: migration_will_fail
- `fails_unique_after_backfill`: migration_will_fail
- `fails_validate_violating_check`: migration_will_fail
- `multi_risk_release`: migration_will_fail

**baseline** — 8 missed risk(s)
- `add_column_not_null`: migration_will_fail
- `add_unique_constraint`: blocking_index_build
- `fails_drop_column_dependent_view`: destructive_drop, missing_lock_timeout
- `multi_risk_release`: migration_will_fail, missing_lock_timeout
- `truncate_table`: missing_lock_timeout
- `type_change_rewrite`: table_rewrite

**advanced** — 5 missed risk(s)
- `add_column_not_null`: not_null_without_default
- `multi_risk_release`: blocking_index_build, missing_lock_timeout, not_null_without_default
- `type_change_rewrite`: table_rewrite
