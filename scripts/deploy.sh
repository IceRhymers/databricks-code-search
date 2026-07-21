#!/usr/bin/env bash
#
# deploy.sh — the ordering engine for the code-search bundle (issue #12).
#
# Usage: deploy.sh <full|destroy> [TARGET]   (TARGET defaults to dev)
#
#   full     validate -> deploy -> secret-check -> migrate(schema) -> run+activate ->
#            grants(post-activation) -> optional first index -> banner.
#   destroy  typed-confirm teardown of the whole bundle (irreversible Lakebase data loss).
#
# Single commands (smoke, index) live in the Makefile; this script owns only the ordering
# that must not be reordered. Every derived value is guarded (jq -er / non-empty / != null)
# so a silent "null" never leaks downstream into a grant target.

set -euo pipefail

die() {
	echo "deploy: $*" >&2
	exit 1
}

# req <value> <human name>: fail loudly on an empty/"null" derived value, else echo it back.
# jq -r prints "null" and exits 0 on a missing field (invisible to set -e); this is the net.
req() {
	[ -n "$1" ] && [ "$1" != null ] || die "could not derive $2 (empty/null)"
	printf '%s' "$1"
}

# retry <attempts> <sleep_s> <cmd...>: run cmd, retrying up to <attempts> times with
# <sleep_s> between tries. Returns the last attempt's status. Used to absorb transient lag
# (e.g. the app SP role becoming visible in pg_roles shortly after the app reaches ACTIVE).
retry() {
	local attempts="$1" sleep_s="$2" i
	shift 2
	for i in $(seq 1 "$attempts"); do
		if "$@"; then
			return 0
		fi
		if [ "$i" -lt "$attempts" ]; then
			echo "deploy: attempt $i/$attempts failed; retrying in ${sleep_s}s" >&2
			sleep "$sleep_s"
		fi
	done
	return 1
}

# grant_attempt <app_role> <job_role> <target>: one idempotent grants pass. An empty job role
# is falsy in migrate.py (`if job_env:` skips it), so dev grants the app only. Args are passed
# explicitly (not via dynamic scope) so retry can re-invoke it cleanly.
grant_attempt() {
	APP_SP_ROLE="$1" JOB_WRITER_ROLE="$2" make migrate TARGET="$3" ARGS=--apply-grants
}

SUB="${1:-}"
TARGET="${2:-dev}"

[ -n "$SUB" ] || die "usage: deploy.sh <full|destroy> [TARGET]"

# Prod threads --var job_run_as_sp (the writer SP); dev has no vars. One derivation idiom,
# mirrored from the `migrate` Makefile target.
VAR_ARGS=()
[ "$TARGET" = prod ] && VAR_ARGS=(--var "job_run_as_sp=${JOB_RUN_AS_SP:-}")

JSON="$(databricks bundle validate -t "$TARGET" ${VAR_ARGS[@]+"${VAR_ARGS[@]}"} -o json 2>/dev/null)" ||
	die "bundle validate -t $TARGET failed (run it directly to see why)"
[ -n "$JSON" ] || die "empty bundle JSON for target $TARGET"

# Guarded readers over the validated bundle JSON. The key is passed via argv (never
# interpolated into the Python source), and a JSON null is normalized to an empty string so
# req()'s non-empty guard fires on it (python's print() would otherwise emit the literal
# "None", which slips past a `!= null` check).
jval() {
	printf '%s' "$JSON" |
		python3 -c "import json,sys;v=json.load(sys.stdin)['variables'][sys.argv[1]]['value'];print('' if v is None else v)" "$1"
}
jworkspace() {
	printf '%s' "$JSON" |
		python3 -c "import json,sys;v=json.load(sys.stdin)['workspace'][sys.argv[1]];print('' if v is None else v)" "$1"
}

# wait_active <app_name>: bounded activation probe (~10x15s); echoes the last observed state.
wait_active() {
	local app="$1" state="" i
	for i in $(seq 1 10); do
		state=$(databricks apps get "$app" -o json 2>/dev/null |
			jq -r '.compute_status.state // empty' || true)
		if [ "$state" = ACTIVE ]; then
			printf '%s' "$state"
			return 0
		fi
		echo "deploy: app '$app' state=${state:-unknown}; waiting (${i}/10)" >&2
		sleep 15
	done
	printf '%s' "$state"
}

cmd_full() {
	local app_name webui_app_name scope key file_path state app_sp_role webui_sp_role url webui_url

	# 1. Validate — already run above (the JSON derivation is the exit-0 gate).
	if [ "$TARGET" = prod ]; then
		# Mirror the deploy-prod guard: an empty value would create a broken NO-LOGIN role.
		[ -n "${JOB_RUN_AS_SP:-}" ] ||
			die "prod deploy requires JOB_RUN_AS_SP=<job run-as SP client id>"
	fi
	app_name=$(req "$(jval app_name)" "app name")
	webui_app_name=$(req "$(jval webui_app_name)" "webui app name")
	scope=$(req "$(jval github_token_secret_scope)" "secret scope")
	key=$(req "$(jval github_token_secret_key)" "secret key")
	file_path=$(req "$(jworkspace file_path)" "workspace file_path")

	# 2. Build the webui wheel + refresh webui/uv.lock BEFORE deploy: Apps source sync (step 3)
	#    uploads whatever is on disk at deploy time. The webui App installs on the uv path
	#    (pyproject.toml + uv.lock, no requirements.txt) so it runs on Python 3.12; a stale/missing
	#    wheel or lock means the webui app can't import `app.*` (or install) at start.
	echo "deploy: [2/11] make webui-wheel"
	make webui-wheel

	# 3. Deploy — ships definitions (Lakebase project/endpoint/catalog, prod job_writer role,
	#    secret scope, job, both app definitions). Does NOT start app compute.
	echo "deploy: [3/11] bundle deploy -t $TARGET"
	databricks bundle deploy -t "$TARGET" ${VAR_ARGS[@]+"${VAR_ARGS[@]}"}

	# 4. GitHub token secret check (indexing is optional for a running app).
	echo "deploy: [4/11] GitHub token secret check (scope=$scope key=$key)"
	if databricks secrets list-secrets "$scope" -o json 2>/dev/null |
		jq -e --arg k "$key" 'any(.[]; .key==$k)' >/dev/null 2>&1; then
		echo "deploy: secret '$key' already present in scope '$scope'"
	elif [ -n "${GITHUB_TOKEN:-}" ]; then
		make set-secrets SECRET_SCOPE="$scope" SECRET_KEY="$key"
	else
		echo "deploy: WARNING indexer will find no token — set GITHUB_TOKEN + 'make set-secrets'," \
			"or re-run deploy after setting it"
	fi

	# 5. Migrate (schema only) — alembic upgrade head as the developer identity. No grants yet
	#    (Decision A1): neither app's SP pg role exists until its first activation (steps 6, 8).
	#    The developer thereby owns the tables and needs no later job grant on dev.
	echo "deploy: [5/11] make migrate (schema only)"
	make migrate TARGET="$TARGET"

	# 6. Run the MCP app — ships app source AND starts compute; the app SP + its pg role
	#    materialize here.
	echo "deploy: [6/11] bundle run code_search (ship source + start compute)"
	databricks bundle run code_search -t "$TARGET" ${VAR_ARGS[@]+"${VAR_ARGS[@]}"}
	state=$(wait_active "$app_name")
	if [ "$state" != ACTIVE ]; then
		# First-activation fallback (Pre-mortem #1): push source directly, re-run, re-probe.
		echo "deploy: app did not activate via bundle run; falling back to apps deploy" >&2
		databricks apps deploy "$app_name" --source-code-path "$file_path/app"
		databricks bundle run code_search -t "$TARGET" ${VAR_ARGS[@]+"${VAR_ARGS[@]}"}
		state=$(wait_active "$app_name")
		[ "$state" = ACTIVE ] ||
			die "app '$app_name' never reached ACTIVE (last state: ${state:-unknown})"
	fi

	# 7. Grants for the MCP app (post-activation, Decision C1 + D1). APP_SP_ROLE is derived
	#    FRESH from apps get at this moment and guarded by req before it can ever reach
	#    validate_role.
	echo "deploy: [7/11] grants (post-activation)"
	app_sp_role=$(req "$(databricks apps get "$app_name" -o json |
		jq -er '.service_principal_client_id')" "app SP client id")
	# dev grants the app only (job_writer_role="" is falsy → migrate.py's `if job_env:` skips
	# it, so a stray JOB_WRITER_ROLE in the developer's shell can't leak a job grant, Addendum 3).
	local job_writer_role=""
	if [ "$TARGET" = prod ]; then
		# prod's JOB_WRITER_ROLE comes from the guarded env, NOT bundle-JSON (which resolves
		# job_run_as_sp to its "" default without --var). D1: assert BOTH before granting so a
		# missing writer role can't silently leave the indexer SP write-less (Pre-mortem #5).
		job_writer_role="$JOB_RUN_AS_SP"
		{ [ -n "$app_sp_role" ] && [ -n "$job_writer_role" ]; } ||
			die "prod grants require BOTH APP_SP_ROLE and JOB_WRITER_ROLE"
	fi
	# NOTE (Addendum 2): `make migrate TARGET=prod` re-runs `bundle validate -t prod` WITHOUT
	# --var job_run_as_sp; harmless — it derives only endpoint/db (independent of job_run_as_sp)
	# and the "" default validates green (databricks.yml:43).
	# Retry to absorb the lag between the app reaching ACTIVE and its SP role becoming visible in
	# pg_roles (Pre-mortem #2); grants + `upgrade head` are idempotent, so re-tries are safe.
	retry 5 10 grant_attempt "$app_sp_role" "$job_writer_role" "$TARGET" ||
		die "grants failed after retries (is the app SP role visible in pg_roles yet?)"

	# 8. Run webui (#35) — same shape as step 6, second app, second SP, same first-activation
	#    fallback.
	echo "deploy: [8/11] bundle run webui (ship source + start compute)"
	databricks bundle run webui -t "$TARGET" ${VAR_ARGS[@]+"${VAR_ARGS[@]}"}
	state=$(wait_active "$webui_app_name")
	if [ "$state" != ACTIVE ]; then
		echo "deploy: webui did not activate via bundle run; falling back to apps deploy" >&2
		databricks apps deploy "$webui_app_name" --source-code-path "$file_path/webui"
		databricks bundle run webui -t "$TARGET" ${VAR_ARGS[@]+"${VAR_ARGS[@]}"}
		state=$(wait_active "$webui_app_name")
		[ "$state" = ACTIVE ] ||
			die "app '$webui_app_name' never reached ACTIVE (last state: ${state:-unknown})"
	fi

	# 9. Grants for webui (post-activation) — same read-only grant as the MCP app's SP (step 7),
	#    applied to webui's own SP. No job-writer role here: the job grant was already applied
	#    in step 7 and is not re-applied per app (grant_attempt's job_role arg is "" → skipped,
	#    same falsy-skip behavior as dev in step 7).
	echo "deploy: [9/11] webui grants (post-activation)"
	webui_sp_role=$(req "$(databricks apps get "$webui_app_name" -o json |
		jq -er '.service_principal_client_id')" "webui app SP client id")
	retry 5 10 grant_attempt "$webui_sp_role" "" "$TARGET" ||
		die "webui grants failed after retries (is the webui app SP role visible in pg_roles yet?)"

	# 10. First index — the config is the only source of truth, and only the job can resolve it
	#     (expanding orgs/users needs the GitHub API + the secret + the pydantic filters).
	#     Non-fatal: a missing GitHub token (step 4 only warns) must not abort a deploy whose
	#     apps are already ACTIVE and granted.
	echo "deploy: [10/11] first index"
	if databricks bundle run code_search_index -t "$TARGET" ${VAR_ARGS[@]+"${VAR_ARGS[@]}"}; then
		echo "deploy: first index complete"
	else
		echo "deploy: WARNING first index failed — check config.yaml and the GitHub token," \
			"then re-run 'make index TARGET=$TARGET'" >&2
	fi

	# 11. Final banner.
	url=$(req "$(databricks apps get "$app_name" -o json | jq -er '.url')" "app url")
	webui_url=$(req "$(databricks apps get "$webui_app_name" -o json | jq -er '.url')" "webui app url")
	echo "deploy: [11/11] DONE — app URL: $url"
	echo "deploy: DONE — webui URL: $webui_url"
	echo "deploy: reminder — the account-admin OAuth app connection (M2M) for the custom MCP app"
	echo "deploy: cannot be created by the bundle; an account admin must create it before external"
	echo "deploy: MCP clients (or the automated smoke MCP leg) can authenticate. The webui app"
	echo "deploy: needs no such connection — it is a plain browser-facing app gated by CAN_USE."
}

cmd_destroy() {
	local project endpoint_name scope app_name webui_app_name catalog ans
	project=$(req "$(jval lakebase_project_name)" "lakebase project name")
	endpoint_name=$(req "$(jval lakebase_endpoint_name)" "lakebase endpoint name")
	scope=$(req "$(jval github_token_secret_scope)" "secret scope")
	app_name=$(req "$(jval app_name)" "app name")
	webui_app_name=$(req "$(jval webui_app_name)" "webui app name")
	catalog=$(req "$(jval catalog_name)" "catalog name")

	echo "This will run 'databricks bundle destroy -t $TARGET --auto-approve'."
	echo "DESTROYS: Lakebase project '$project' (production branch + endpoint '$endpoint_name')"
	echo "  and ALL indexed data (repos/files/symbols), the UC catalog '$catalog',"
	echo "  the secret scope '$scope', both apps ('$app_name', '$webui_app_name'), and the"
	echo "  indexing job. NOT RECOVERABLE."
	printf 'Type the project name (%s) to confirm: ' "$project"
	read -r ans
	[ "$ans" = "$project" ] || die "confirmation mismatch; aborting"
	# --auto-approve: bundle destroy prompts again otherwise, after our own typed gate.
	databricks bundle destroy -t "$TARGET" ${VAR_ARGS[@]+"${VAR_ARGS[@]}"} --auto-approve
}

case "$SUB" in
full) cmd_full ;;
destroy) cmd_destroy ;;
*) die "unknown subcommand '$SUB' (expected: full | destroy)" ;;
esac
