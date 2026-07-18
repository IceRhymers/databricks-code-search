.DEFAULT_GOAL := help

.PHONY: install test test-integration lint fmt fmt-check requirements clean help migrate migrate-local migration set-secrets deploy deploy-prod

# Secret scope/key for `set-secrets`. These MUST match the bundle variables
# `github_token_secret_scope` / `github_token_secret_key` in databricks.yml
# (override both here and via --var together if you change them).
SECRET_SCOPE ?= code-search
SECRET_KEY   ?= github_token

install: ## Install all dependencies (incl. dev group)
	uv sync --all-groups

test: ## Run unit tests
	uv run pytest -m unit

test-integration: ## Run integration tests
	uv run pytest -m integration

lint: ## Run ruff check + format check + mypy
	uv run ruff check . && uv run ruff format --check . && uv run mypy app indexer

fmt: ## Auto-format code with ruff
	uv run ruff format .

fmt-check: ## Check formatting without modifying files
	uv run ruff format --check .

migrate: ## Apply schema migrations (Lakebase or PGHOST); grants opt-in via ARGS=--apply-grants
	uv run python scripts/migrate.py $(ARGS)

migrate-local: ## Apply migrations against local Postgres (run under PGHOST; grants skipped)
	uv run python scripts/migrate.py

migration: ## Autogenerate a revision (local only): make migration MSG="message"
	@test -n "$(MSG)" || (echo "migration requires MSG=\"...\"" && exit 1)
	@test -n "$$PGHOST" || (echo "migration requires PGHOST (local); never autogenerate against live Lakebase" && exit 1)
	uv run alembic revision --autogenerate -m "$(MSG)"

deploy: ## Deploy the bundle to the dev target
	databricks bundle deploy -t dev

deploy-prod: ## Deploy to prod (requires JOB_RUN_AS_SP=<job run-as SP client id>)
	@test -n "$$JOB_RUN_AS_SP" || (echo "deploy-prod requires JOB_RUN_AS_SP=<client-id> (the job run-as SP); an empty value creates a broken NO-LOGIN role" && exit 1)
	databricks bundle deploy -t prod --var job_run_as_sp="$$JOB_RUN_AS_SP"

set-secrets: ## Write the GitHub token into the bundle's secret scope (run after deploy). Requires GITHUB_TOKEN; scope/key via SECRET_SCOPE/SECRET_KEY.
	@test -n "$$GITHUB_TOKEN" || (echo "set-secrets requires GITHUB_TOKEN in env" && exit 1)
	databricks secrets put-secret $(SECRET_SCOPE) $(SECRET_KEY) --string-value "$$GITHUB_TOKEN"

requirements: ## Export production requirements.txt for the app
	uv export --no-dev --no-hashes -o app/requirements.txt

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
