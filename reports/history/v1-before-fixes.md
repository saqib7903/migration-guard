# MigrationGuard — baseline vs advanced

- generated: `20260830-123221`
- model: `claude-sonnet-4-5` · provider: `claude_cli` · shadow rows/table: `20000`
- cases: **29** (21 labelled unsafe, 8 labelled safe)

| Metric | rules_only | baseline | advanced |
|---|---|---|---|
| Risk recall | 0.878 | 0.878 | 0.61 |
| Risk precision | 0.973 | 0.529 | 0.758 |
| Risk F1 | 0.923 | 0.661 | 0.676 |
| Decision accuracy | 0.724 | 0.724 | 0.621 |
| Outage misses (unsafe called safe) | 2 | 0 | 0 |
| False-alarm risks on safe cases | 1 | 8 | 0 |
| Findings with no tool evidence | 0 | 71 | 0 |
| Rewrites offered | 0 | 0 | 14 |
| Rewrites validated by execution | 0 | 0 | 14 |
| LLM calls | 0 | 29 | 106 |
| Wall seconds | 0.1 | 1353.9 | 2428.9 |
| Estimated cost (USD) | 0.0 | 1.6231 | 4.4518 |
| Harness errors | 0 | 0 | 10 |

## Per-case decisions

| Case | expected | rules_only | baseline | advanced |
|---|---|---|---|---|
| `add_column_not_null` | block | ❌ needs_changes | ✅ block | ✅ block |
| `add_unique_constraint` | needs_changes | ✅ needs_changes | ❌ block | ✅ needs_changes |
| `blocking_index` | needs_changes | ✅ needs_changes | ✅ needs_changes | ✅ needs_changes |
| `concurrently_in_txn` | block | ✅ block | ✅ block | ❌ ERROR |
| `drop_column` | block | ✅ block | ✅ block | ✅ block |
| `drop_index_blocking` | needs_changes | ✅ needs_changes | ✅ needs_changes | ✅ needs_changes |
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
| `safe_concurrent_index` | safe | ✅ safe | ❌ needs_changes | ✅ safe |
| `safe_create_table` | safe | ✅ safe | ❌ needs_changes | ✅ safe |
| `safe_drop_index_concurrently` | safe | ✅ safe | ❌ needs_changes | ✅ safe |
| `safe_not_valid_constraint` | safe | ✅ safe | ✅ safe | ❌ ERROR |
| `safe_set_not_null_after_check` | safe | ❌ needs_changes | ❌ needs_changes | ❌ ERROR |
| `safe_widen_varchar` | safe | ✅ safe | ✅ safe | ❌ ERROR |
| `set_not_null_scan` | block | ❌ needs_changes | ✅ block | ❌ ERROR |
| `truncate_table` | block | ✅ block | ✅ block | ❌ ERROR |
| `type_change_rewrite` | block | ✅ block | ✅ block | ❌ ERROR |
| `unbounded_backfill` | needs_changes | ✅ needs_changes | ❌ block | ❌ ERROR |
| `validating_fk` | needs_changes | ✅ needs_changes | ❌ block | ❌ ERROR |
| `volatile_default` | needs_changes | ✅ needs_changes | ❌ block | ❌ ERROR |

## Missed risks (false negatives) per system

**rules_only** — 5 missed risk(s)
- `fails_drop_column_dependent_view`: migration_will_fail
- `fails_missing_column`: migration_will_fail
- `fails_typo_syntax`: migration_will_fail
- `fails_unique_after_backfill`: migration_will_fail
- `fails_validate_violating_check`: migration_will_fail

**baseline** — 5 missed risk(s)
- `add_unique_constraint`: blocking_index_build
- `drop_index_blocking`: blocking_index_build
- `multi_risk_release`: missing_lock_timeout, not_null_without_default
- `type_change_rewrite`: unsafe_type_change

**advanced** — 16 missed risk(s)
- `add_column_not_null`: not_null_without_default
- `concurrently_in_txn`: non_transactional_in_txn
- `drop_index_blocking`: blocking_index_build
- `multi_risk_release`: blocking_index_build
- `set_not_null_scan`: missing_lock_timeout, not_null_without_default
- `truncate_table`: destructive_drop, missing_lock_timeout
- `type_change_rewrite`: missing_lock_timeout, table_rewrite, unsafe_type_change
- `unbounded_backfill`: long_running_backfill
- `validating_fk`: missing_lock_timeout, validating_constraint
- `volatile_default`: missing_lock_timeout, table_rewrite
