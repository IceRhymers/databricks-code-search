.DEFAULT_GOAL := help

.PHONY: install run test test-integration lint fmt fmt-check requirements clean help migrate migrate-semantic migrate-local migration set-secrets deploy deploy-prod smoke index destroy

# Secret scope/key for `set-secrets`. These MUST match the bundle variables
# `github_token_secret_scope` / `github_token_secret_key` in databricks.yml
# (override both here and via --var together if you change them).
SECRET_SCOPE ?= code-search
SECRET_KEY   ?= github_token

# Bundle target that Lakebase-facing commands (migrate) resolve their connection
# from. Override: `make migrate TARGET=prod`.
TARGET ?= dev

install: ## Install all dependencies (incl. dev group)
	uv sync --all-groups

run: ## Run the MCP server locally (dual-mode: set PGHOST + PG* for local Postgres; binds DATABRICKS_APP_PORT or 8000)
	uv run sh -c 'uvicorn app.main:app --host 0.0.0.0 --port $${DATABRICKS_APP_PORT:-8000}'

test: ## Run unit tests (no external deps: unit + pure observability/logging tests)
	uv run pytest -m "unit or observability"

test-integration: ## Run integration tests (need Postgres: integration + streamable-HTTP e2e)
	uv run pytest -m "integration or e2e"

lint: ## Run ruff check + format check + mypy
	uv run ruff check . && uv run ruff format --check . && uv run mypy app indexer

fmt: ## Auto-format code with ruff
	uv run ruff format .

fmt-check: ## Check formatting without modifying files
	uv run ruff format --check .

migrate: ## Apply migrations to the TARGET's Lakebase (TARGET=dev|prod, default dev); grants via ARGS=--apply-grants
	@test -z "$$PGHOST" || (echo "PGHOST is set -> migrate would hit local Postgres. Use 'make migrate-local', or unset PGHOST to target the bundle's Lakebase." && exit 1)
	@JSON="$$(databricks bundle validate -t $(TARGET) -o json 2>/dev/null)" || true; \
	test -n "$$JSON" || { echo "could not read bundle target '$(TARGET)' (try: databricks bundle validate -t $(TARGET))"; exit 1; }; \
	EP="$$(printf '%s' "$$JSON" | python3 -c 'import json,sys;v=json.load(sys.stdin)["variables"];print("projects/%s/branches/production/endpoints/%s"%(v["lakebase_project_name"]["value"],v["lakebase_endpoint_name"]["value"]))')"; \
	DB="$$(printf '%s' "$$JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["variables"]["database_name"]["value"])')"; \
	echo "-> migrating target '$(TARGET)' against $$EP (db=$$DB)"; \
	LAKEBASE_ENDPOINT="$$EP" LAKEBASE_DATABASE="$$DB" uv run python scripts/migrate.py $(ARGS)

migrate-semantic: ## Apply the GATED semantic revision to TARGET's Lakebase (issue #14; separate version table). IRREVERSIBLE prerequisite: the Databricks-managed shared_preload_libraries MUST already include lakebase_vector,lakebase_text.
	@test -z "$$PGHOST" || (echo "PGHOST is set -> migrate-semantic would hit local Postgres. The gated lakebase_* DDL only runs against an enabled Lakebase project; unset PGHOST to target the bundle's Lakebase." && exit 1)
	@echo "!! migrate-semantic creates the 'chunks' table with the BETA lakebase_vector/lakebase_text extensions."
	@echo "!! PREREQUISITE (irreversible, project-level, out-of-band): the Databricks-managed"
	@echo "!! shared_preload_libraries for this Lakebase project MUST already include lakebase_vector,lakebase_text."
	@echo "!! Without it, CREATE EXTENSION fails loudly. See docs/runbooks/semantic-enablement.md."
	@printf 'Type "enable-semantic" to proceed: '; read ack; test "$$ack" = "enable-semantic" || { echo "aborted."; exit 1; }
	@JSON="$$(databricks bundle validate -t $(TARGET) -o json 2>/dev/null)" || true; \
	test -n "$$JSON" || { echo "could not read bundle target '$(TARGET)' (try: databricks bundle validate -t $(TARGET))"; exit 1; }; \
	EP="$$(printf '%s' "$$JSON" | python3 -c 'import json,sys;v=json.load(sys.stdin)["variables"];print("projects/%s/branches/production/endpoints/%s"%(v["lakebase_project_name"]["value"],v["lakebase_endpoint_name"]["value"]))')"; \
	DB="$$(printf '%s' "$$JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["variables"]["database_name"]["value"])')"; \
	echo "-> semantic migrating target '$(TARGET)' against $$EP (db=$$DB)"; \
	LAKEBASE_ENDPOINT="$$EP" LAKEBASE_DATABASE="$$DB" CODE_SEARCH_SEMANTIC_ACK=1 uv run python scripts/migrate.py --semantic

migrate-local: ## Apply migrations against local Postgres (run under PGHOST; grants skipped)
	uv run python scripts/migrate.py

migration: ## Autogenerate a revision (local only): make migration MSG="message"
	@test -n "$(MSG)" || (echo "migration requires MSG=\"...\"" && exit 1)
	@test -n "$$PGHOST" || (echo "migration requires PGHOST (local); never autogenerate against live Lakebase" && exit 1)
	uv run alembic revision --autogenerate -m "$(MSG)"

deploy: ## Deploy + activate the app on TARGET via the full ordered pipeline (scripts/deploy.sh)
	bash scripts/deploy.sh full $(TARGET)

deploy-prod: ## Deploy + activate on prod (requires JOB_RUN_AS_SP=<job run-as SP client id>)
	@test -n "$$JOB_RUN_AS_SP" || (echo "deploy-prod requires JOB_RUN_AS_SP=<client-id> (the job run-as SP); an empty value creates a broken NO-LOGIN role" && exit 1)
	TARGET=prod bash scripts/deploy.sh full prod

smoke: ## Smoke-test the deployed app (TARGET=dev|prod; ARGS=--expect-indexed/--enable-mcp)
	@test -z "$$PGHOST" || (echo "PGHOST is set -> smoke would hit local Postgres. Unset PGHOST to target the bundle's Lakebase." && exit 1)
	@JSON="$$(databricks bundle validate -t $(TARGET) -o json 2>/dev/null)" || true; \
	test -n "$$JSON" || { echo "could not read bundle target '$(TARGET)' (try: databricks bundle validate -t $(TARGET))"; exit 1; }; \
	APP="$$(printf '%s' "$$JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["variables"]["app_name"]["value"])')"; \
	EP="$$(printf '%s' "$$JSON" | python3 -c 'import json,sys;v=json.load(sys.stdin)["variables"];print("projects/%s/branches/production/endpoints/%s"%(v["lakebase_project_name"]["value"],v["lakebase_endpoint_name"]["value"]))')"; \
	DB="$$(printf '%s' "$$JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["variables"]["database_name"]["value"])')"; \
	URL="$$(databricks apps get "$$APP" -o json | jq -er '.url')" || { echo "could not resolve app URL for '$$APP' (is it deployed?)"; exit 1; }; \
	echo "-> smoke '$(TARGET)' against $$URL (endpoint=$$EP db=$$DB)"; \
	LAKEBASE_ENDPOINT="$$EP" LAKEBASE_DATABASE="$$DB" uv run python scripts/smoke.py --app-url "$$URL" $(ARGS)

index: ## Run the indexing job on TARGET (populates repos/files/symbols from configured repos)
	databricks bundle run code_search_index -t $(TARGET)

destroy: ## Tear down the whole bundle for TARGET (typed-confirm; irreversible Lakebase data loss)
	bash scripts/deploy.sh destroy $(TARGET)

set-secrets: ## Write the GitHub token into the bundle's secret scope (run after deploy). Requires GITHUB_TOKEN; scope/key via SECRET_SCOPE/SECRET_KEY.
	@test -n "$$GITHUB_TOKEN" || (echo "set-secrets requires GITHUB_TOKEN in env" && exit 1)
	databricks secrets put-secret "$(SECRET_SCOPE)" "$(SECRET_KEY)" --string-value "$$GITHUB_TOKEN"

requirements: ## Export production requirements.txt for the app (no `-e .`: the app runs from shipped source, not an installed package)
	uv export --no-dev --no-hashes --no-emit-project -o app/requirements.txt

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
