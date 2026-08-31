SHELL := /bin/bash
PY := python3
export PYTHONPATH := src

.DEFAULT_GOAL := help

help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## install python dependencies
	$(PY) -m pip install -r requirements.txt

db-up: ## start the disposable shadow postgres on :5433
	docker compose up -d --wait shadow-postgres

db-down: ## stop and delete the shadow postgres
	docker compose down -v

dataset: ## regenerate eval/dataset from scripts/make_dataset.py
	$(PY) scripts/make_dataset.py

doctor: ## check the environment (model provider, cassettes, shadow db)
	$(PY) -m migration_guard.cli doctor

test: ## unit tests, no model and no database required
	$(PY) -m pytest tests -q

smoke: ## end-to-end sanity run with no model at all
	MG_LLM_PROVIDER=stub $(PY) -m migration_guard.cli review --case add_column_not_null --system rules_only

review: ## review one case, e.g. make review CASE=multi_risk_release
	$(PY) -m migration_guard.cli review --case $(or $(CASE),add_column_not_null) --system $(or $(SYSTEM),advanced)

eval-replay: ## THE HEADLINE RESULT - reproduces reports/ from cassettes, offline and free
	MG_LLM_PROVIDER=replay $(PY) -m migration_guard.cli eval --systems rules_only,baseline,advanced

eval-live: ## re-record every model call (costs money, needs `claude` or an API key)
	MG_LLM_PROVIDER=$(or $(PROVIDER),claude_cli) MG_EVAL_WORKERS=$(or $(WORKERS),5) \
	  $(PY) -m migration_guard.cli eval --systems rules_only,baseline,advanced

ablations: ## the verifier ablation quoted in the improvement changelog (replayed, free)
	@echo "== control: verifier ON, rewriter off =="
	@MG_LLM_PROVIDER=replay MG_ENABLE_REWRITER=0 MG_ENABLE_VERIFIER=1 \
	  MG_REPORTS_DIR=reports/ablation-verifier-on \
	  $(PY) -m migration_guard.cli eval --systems advanced
	@echo "== ablation: verifier OFF =="
	@MG_LLM_PROVIDER=replay MG_ENABLE_REWRITER=0 MG_ENABLE_VERIFIER=0 \
	  MG_REPORTS_DIR=reports/ablation-verifier-off \
	  $(PY) -m migration_guard.cli eval --systems advanced

readme: ## regenerate the README results table from reports/comparison.json
	$(PY) scripts/sync_readme_numbers.py

verify-repro: ## fail if a replay run does not match the published numbers
	$(PY) scripts/check_reproduction.py

trajectories: ## rebuild docs/TRAJECTORIES.md from the recorded runs
	$(PY) scripts/export_trajectories.py

clean: ## remove generated runs
	rm -rf runs/* reports/ablation-*

.PHONY: help install db-up db-down dataset doctor test smoke review eval-replay eval-live ablations readme verify-repro trajectories clean
