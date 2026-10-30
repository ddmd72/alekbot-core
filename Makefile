# Alek-Core Makefile
# Best Practices: https://makefiletutorial.com/
#
# Single live environment. The Cloud Run service is named `alek-bot-dev` and the
# build config is `cloudbuild-dev.yaml` for historical reasons (the separate prod
# deployment was retired 2026-05-31); the names are kept because a Cloud Run rename
# = a new service + new URL, not worth it. See
# docs/04_solution_strategy/decisions/dead_prod_collections_deletion.md.

# ============================================================================
# CONFIGURATION VARIABLES
# ============================================================================

# Load environment variables from .env if present
ifneq (,$(wildcard .env))
    include .env
    export
endif

# Project Configuration
PROJECT_ID ?= $(GOOGLE_CLOUD_PROJECT)
REGION ?= us-central1
PYTHON ?= python3

# The single live Cloud Run service + its async research job + the voice relay
# and the drive indexer (more Cloud Run services, same image, different
# entrypoints — see cloudbuild-dev.yaml, relay_main.py and indexer_main.py).
SERVICE_NAME ?= alek-bot-dev
RESEARCH_JOB ?= alek-research-job-dev
VOICE_RELAY ?= alek-voice-relay-dev
INDEXER ?= alek-bot-indexer-dev

# Cloud Run service URL — defined in .env (gitignored), loaded via include above.
# Required: SERVICE_URL_DEV. The deploy-time OAuth callback is derived from it in
# the deploy target's substitutions — NOT from the local OAUTH_REDIRECT_URI in
# .env (that one is a localhost value, used only for running the app locally).

# Voice relay URL — same category as SERVICE_URL_DEV: a human-maintained .env
# value turned into a Cloud Build substitution, never read directly from .env by
# the app (CLAUDE.md, "Deploy-substitution trap"). Chicken-and-egg on a first
# deploy: alek-voice-relay-dev has no URL until it exists, so deploy once, read
# the URL back, set VOICE_RELAY_URL_DEV, deploy again — see
# docs/07_deployment/README.md § Voice Relay Stream URL.
#
# The app needs the WebSocket form (Twilio's <Stream url="wss://...">), while
# gcloud reports the https:// one — patsubst converts it, so .env holds exactly
# what `gcloud run services describe` prints and nothing has to be hand-edited.
_VOICE_RELAY_URL = $(patsubst https://%,wss://%,$(VOICE_RELAY_URL_DEV))

# Drive indexer URL (USER_DRIVE_INDEXING_RFC §12.5) — same category as
# VOICE_RELAY_URL_DEV: a human-maintained .env value that reaches the deploy only
# as the Cloud Build substitution _INDEXER_URL, which cloudbuild-dev.yaml sets as
# DRIVE_INDEXER_URL on BOTH services (the main service enqueues drive_sync to it;
# the indexer enqueues its own continuations to it). Never a dual-purpose key read
# from local .env (CLAUDE.md, "Deploy-substitution trap"). It is a declared
# exception to RFC §12.4a (RFC Δ21), like VOICE_RELAY_URL_DEV.
# Prerequisite, once: the Cloud Build service account needs
# roles/cloudtasks.queueAdmin to create/update drive-sync-dev (SCHEDULERS.md §
# Start Drive Sync). Two-pass first deploy: alek-bot-indexer-dev has no URL until
# it exists, so
#   1. `DRIVE_FIRST_PASS=1 make deploy` — creates the queue, the indexer (after
#      the main service) and the scheduler job; the main service gets an empty
#      DRIVE_INDEXER_URL (no nudges, start_drive_sync answers "disabled") and the
#      indexer's /worker answers 503;
#   2. `gcloud run services describe alek-bot-indexer-dev --region=us-central1
#      --format='value(status.url)'` and put it in .env as INDEXER_URL_DEV
#      (the https:// form, no trailing slash);
#   3. `make deploy` again.
# Every other deploy refuses to run with INDEXER_URL_DEV empty: an empty value
# disables indexing in the live environment without any alert (a checkout
# without the real .env, a key lost in an edit).
_INDEXER_URL = $(INDEXER_URL_DEV)

# Untrusted-content sandbox, render family (UNTRUSTED_CONTENT_SANDBOX_RFC §4.12). Each value has its own .env key and its
# own Cloud Build substitution (never reuse a local key: the deploy-substitution trap). The switch defaults to `inprocess`,
# so deploy does NOT refuse an empty URL; the sandbox build/deploy steps skip while SANDBOX_RENDER_SA_DEV is empty. First
# deploy is two-pass (SA, then URL): docs/07_deployment/README.md.
_SANDBOX_RENDER = $(or $(SANDBOX_RENDER_DEV),inprocess)
_SANDBOX_RENDER_URL = $(SANDBOX_RENDER_URL_DEV)
_SANDBOX_RENDER_SA = $(SANDBOX_RENDER_SA_DEV)
_SANDBOX_RENDER_CALLERS = $(SANDBOX_RENDER_CALLERS_DEV)

# The indexer's upgrade-pass emergency switch (USER_DRIVE_INDEXING_RFC §25.8): DRIVE_UPGRADE_PASS_DEV=off in .env
# keeps it off across deploys (the indexer step's --set-env-vars replaces every variable, so a switch set with
# `gcloud run services update` alone would turn back on at the next deploy). Default on. A deploy-only key, never
# the local DRIVE_UPGRADE_PASS (CLAUDE.md, "Deploy-substitution trap").
_DRIVE_UPGRADE_PASS = $(or $(DRIVE_UPGRADE_PASS_DEV),on)

# The owner-notes kill switch (DRIVE_FILE_NOTES_RFC §3.6): DRIVE_NOTES_ENABLED_DEV=off in .env keeps it off across
# deploys, on the main service AND the indexer (one substitution, so both switch and roll back together). Default on,
# like the code. A deploy-only key, never the local DRIVE_NOTES_ENABLED (CLAUDE.md, "Deploy-substitution trap").
_DRIVE_NOTES_ENABLED = $(or $(DRIVE_NOTES_ENABLED_DEV),on)

# Transcription of audio and video (USER_DRIVE_INDEXING_RFC §27.13). DRIVE_TRANSCRIBE_ENABLED_DEV is REQUIRED in .env and must be exactly on or off
# (`make deploy` refuses otherwise, T-R34); the switch is on only for `on`; DRIVE_TRANSCRIBE_USD_CAP_DEV (a number, empty = the $20 monthly maximum) can only LOWER the cap. Both ride as
# substitutions to the indexer (the cap to the main service too: the Cabinet shows it). Deploy-only keys, never the
# local DRIVE_TRANSCRIBE_ENABLED / DRIVE_TRANSCRIBE_USD_CAP (CLAUDE.md, "Deploy-substitution trap").
_DRIVE_TRANSCRIBE_ENABLED = $(or $(DRIVE_TRANSCRIBE_ENABLED_DEV),off)
_DRIVE_TRANSCRIBE_USD_CAP = $(DRIVE_TRANSCRIBE_USD_CAP_DEV)

# Default entry count for log reads
K ?= 300

# ============================================================================
# PHONY TARGETS (targets that don't create files)
# ============================================================================

.PHONY: help
.PHONY: install install-dev clean auth
.PHONY: test test-unit test-coverage test-integration test-cloud test-g2-offline lint format check
.PHONY: deploy deploy-indexes
.PHONY: start stop restart
.PHONY: logs logs-tail logs-tail-clean logs-tail-full logs-perf fetch-logs
.PHONY: logs-mode-clean logs-mode-full
.PHONY: logs-job fetch-logs-job list-jobs logs-execution cancel-job
.PHONY: logs-relay fetch-logs-relay
.PHONY: logs-indexer fetch-logs-indexer
.PHONY: services status
.PHONY: claude-model claude-rollback claude-forward dr-model dr-rollback dr-forward
.PHONY: typecheck check-types
.PHONY: check-models check-pricing openai-model openai-rollback openai-forward
.PHONY: test-e2e-smart test-e2e-quick test-e2e-router test-e2e-consolidation test-e2e-websearch test-e2e-all
.PHONY: delete

# ============================================================================
# DEFAULT TARGET
# ============================================================================

.DEFAULT_GOAL := help

help: ## Show this help message
	@echo "╔════════════════════════════════════════════════════════════════╗"
	@echo "║              🤖 Alek-Core Management Commands                  ║"
	@echo "╚════════════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "📦 INSTALLATION & SETUP:"
	@echo "  make install         Install dependencies"
	@echo "  make install-dev     Install dependencies + test/lint tools"
	@echo "  make clean           Clean up temporary files and caches"
	@echo "  make auth            Authenticate with Google Cloud"
	@echo ""
	@echo "🧪 TESTING & QUALITY:"
	@echo "  make check           ruff lint + mypy + unit/architecture tests (no slow tests, no coverage) + G2"
	@echo "  make test            Run all tests"
	@echo "  make test-unit       Run unit tests"
	@echo "  make test-affected   Inner loop: only the unit tests testmon selects for your changes"
	@echo "  make ci              PR job: lint + mypy + unit suite once (slow tests, coverage gate) + G2"
	@echo "  make test-coverage   Unit tests + per-file coverage gate"
	@echo "  make test-integration Run integration tests"
	@echo "  make test-cloud      Cloud gate: the parity scenarios on the Firestore test database and, with TEST_GCS_BUCKET, the GCS test bucket (serial, by hand)"
	@echo "  make lint            Lint src/ and the root entry points with ruff"
	@echo "  make format          Format src/ with ruff"
	@echo ""
	@echo "🚀 DEPLOY (single live environment — deploy is manual by choice):"
	@echo "  make deploy          Build + deploy to Cloud Run ($(SERVICE_NAME))"
	@echo "  make deploy-indexes  Deploy Firestore indexes"
	@echo ""
	@echo "⚙️  CLOUD OPERATIONS:"
	@echo "  make start           Start service (min-instances=1)"
	@echo "  make stop            Stop service (min-instances=0)"
	@echo "  make restart         Restart service"
	@echo "  make services        Show service URL"
	@echo "  make status          Show service status"
	@echo ""
	@echo "📊 MONITORING & LOGS:"
	@echo "  make logs [K=300]    View last K service log entries"
	@echo "  make logs-tail       Live tail logs"
	@echo "  make logs-tail-clean Live tail logs (clean mode)"
	@echo "  make logs-tail-full  Live tail logs (full mode)"
	@echo "  make logs-perf       Live tail perf logs"
	@echo "  make fetch-logs [K=300]  Fetch last K logs to alek_debug.log"
	@echo "  make logs-mode-clean Set LOG_TRACE_CONTEXT=clean on the service"
	@echo "  make logs-mode-full  Set LOG_TRACE_CONTEXT=full on the service"
	@echo "  make logs-job        View recent Cloud Run Job logs"
	@echo "  make fetch-logs-job [K=300]  Fetch last K job logs to alek_debug_job.log"
	@echo "  make list-jobs       List job executions with status"
	@echo "  make logs-execution EXECUTION=<name>  View logs for a specific execution"
	@echo "  make cancel-job EXECUTION=<name>      Cancel a running execution"
	@echo "  make logs-relay      View recent voice relay service logs"
	@echo "  make fetch-logs-relay [K=300]  Fetch last K relay logs to alek_debug_relay.log"
	@echo "  make logs-indexer    View recent drive indexer service logs"
	@echo "  make fetch-logs-indexer [K=300]  Fetch last K indexer logs to alek_debug_indexer.log"
	@echo ""
	@echo "🗄️  MAINTENANCE:"
	@echo "  make check-models    Check available Gemini models"
	@echo "  make check-pricing   Fetch live model prices and audit billing.py"
	@echo ""
	@echo "🧪 E2E TESTING (real API flow):"
	@echo "  make test-e2e-smart / -quick / -router / -consolidation / -websearch / -all"
	@echo ""
	@echo "🗑️  CLEANUP:"
	@echo "  make delete          Delete the Cloud Run service (DANGEROUS)"
	@echo ""
	@echo "💡 Tip: Use 'make <target>' to run any command"
	@echo ""

# ============================================================================
# INSTALLATION & SETUP
# ============================================================================

install: ## Install dependencies
	@echo "📦 Installing dependencies..."
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

install-dev: install ## Install dependencies + test/lint tools
	@echo "📦 Installing dev tools..."
	$(PYTHON) -m pip install pytest pytest-asyncio pytest-mock pytest-xdist pytest-testmon pytest-cov pytest-socket ruff

clean: ## Clean up temporary files and caches
	@echo "🧹 Cleaning up temporary files..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type f -name ".DS_Store" -delete 2>/dev/null || true
	@echo "✅ Cleanup complete"

auth: ## Authenticate with Google Cloud
	@echo "🔐 Authenticating with Google Cloud..."
	gcloud auth application-default login

# ============================================================================
# TESTING & QUALITY
# ============================================================================

# The unit suite runs in parallel via pytest-xdist: `auto` = one worker per CPU, which adapts to
# the laptop, the CI runner and a cloud session alike; cap it with `make check PYTEST_WORKERS=6`.
# The flag lives here, not in pytest.ini addopts, so a targeted `pytest path::test` stays a single
# fast process. Integration/e2e stay serial — they touch live services.
# Without xdist installed the suite falls back to a serial run, with a warning.
# `slow` tests (over 0.5 s with coverage on) are skipped the same way unless RUN_SLOW_TESTS=1; `make ci` sets it.
# Only one full suite runs at a time on a machine (tests/plugins/suite_lock.py: flock on a file in the temp dir).
# Tests marked `timing` assert on wall-clock/CPU time and fail on a loaded machine with no code change. By owner
# decision (2026-10-20) they are off every everyday run (check, test-unit, plain pytest — tests/conftest.py skips
# them) and run only via `make test-timing`: in CI after a merge to main (job `timing`), or by hand.
PYTEST_WORKERS ?= auto
PYTEST_XDIST = $(shell $(PYTHON) -c "import xdist" 2>/dev/null && echo yes)
# --durations=25 is an advisory report of the slowest tests.
PYTEST_UNIT_FLAGS = $(if $(PYTEST_XDIST),-n $(PYTEST_WORKERS) -q,-v) --durations=25
XDIST_WARNING = $(if $(PYTEST_XDIST),,echo "⚠️  pytest-xdist not installed — running serially (pip install -r requirements.txt)")

test: ## Run all tests
	@echo "🧪 Running all tests..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/ -v --asyncio-mode=auto --ignore=tests/cloud

test-unit: ## Run unit tests
	@echo "🧪 Running unit tests..."
	@$(XDIST_WARNING)
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/unit/ $(PYTEST_UNIT_FLAGS) --asyncio-mode=auto -m "not timing"

test-timing: ## Wall-clock/CPU-time tests only, serial (CI runs it after a merge to main)
	@echo "⏱  Running timing tests..."
	RUN_TIMING_TESTS=1 DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/unit/ -q --asyncio-mode=auto -m timing

# Coverage flags shared by test-coverage and ci. A bare `--cov` takes its sources from .coveragerc (src + the root *_main.py
# entry points, branch coverage on); a `--cov=src` here would replace that list. Gates: the global floor below, the per-file
# table (RFC §8.4) and the per-layer floors (P2).
COVERAGE_FLAGS = --cov --cov-report=term-missing:skip-covered --cov-report=json --cov-fail-under=70

test-coverage: ## Unit tests + global + per-file coverage gate (RFC § 8.4)
	@echo "🧪 Running unit tests with coverage..."
	@$(XDIST_WARNING)
	RUN_SLOW_TESTS=1 DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/unit/ $(PYTEST_UNIT_FLAGS) \
	    --asyncio-mode=auto -m "not timing" $(COVERAGE_FLAGS)
	@echo "🚦 Enforcing per-file coverage thresholds..."
	@$(PYTHON) scripts/check_coverage_thresholds.py
	@echo "🚦 Enforcing per-layer coverage floors..."
	@$(PYTHON) scripts/check_coverage_layers.py

# testmon never runs together with coverage. A fresh worktree gets the main checkout's .testmondata (the first
# entry of `git worktree list`; copied with SQLite's backup API, testmon uses WAL), so its first run is not a cold suite.
# testmon selects by executed Python lines: it can miss a test whose input is a data file or code run in a child
# process: such a test is selected by hand (or runs in `make check` / CI).
# --testmon-forceselect: without it testmon switches selection off whenever -m is given (testmon 2.2.0).
TESTMON_SEED = $(shell git -c safe.directory='*' worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' | head -n 1)

test-affected: ## Inner loop: only the unit tests testmon selects for your changes
	@echo "🎯 Running affected tests..."
	@$(XDIST_WARNING)
	@if [ ! -f .testmondata ] && [ -n "$(TESTMON_SEED)" ] && [ "$(TESTMON_SEED)" != "$(CURDIR)" ] && [ -f "$(TESTMON_SEED)/.testmondata" ]; then \
		echo "🌱 Seeding .testmondata from $(TESTMON_SEED)"; $(PYTHON) scripts/seed_testmondata.py "$(TESTMON_SEED)/.testmondata" .testmondata; fi
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/unit/ -n $(PYTEST_WORKERS) -q --asyncio-mode=auto --testmon --testmon-forceselect -m "not timing"

test-integration: ## Run integration tests
	@echo "🧪 Running integration tests..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/integration/ -v --asyncio-mode=auto

# Cloud gate (RFC §4): real writes to the Firestore TEST database and, with TEST_GCS_BUCKET set, the GCS TEST bucket (the guards
# are in tests/cloud/conftest.py and tests/cloud/test_gcs_parity_cloud.py; GCS skips without the variable); never in
# `make check` / `make ci` / `make test`. Serial. One manual runner at a time: the lease document comes with P12.
test-cloud: ## Cloud gate: the parity scenarios on the Firestore test database and the GCS test bucket (serial)
	@echo "☁️  Running the cloud gate on the test database..."
	RUN_CLOUD_TESTS=1 DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/cloud -q -p no:randomly --asyncio-mode=auto

lint: ## Lint src/ and the root entry points with ruff (pyflakes + pycodestyle errors)
	@echo "🔍 Running ruff linter on src/ and the root entry points..."
	$(PYTHON) -m ruff check src/ main.py indexer_main.py job_main.py relay_main.py sandbox_main.py

format: ## Format src/ with ruff (black-compatible)
	@echo "✨ Formatting src/ with ruff..."
	$(PYTHON) -m ruff format src/

typecheck: ## mypy --strict on src/domain + src/ports (part of `make check` and `make ci`)
	@echo "🔎 Running mypy (strict) on domain/ + ports/..."
	$(PYTHON) -m mypy --config-file mypy.ini

check-types: ## mypy (lenient) on src/adapters — informational, never fails
	@echo "🔎 mypy on adapters/ (informational)..."
	@$(PYTHON) -m mypy --config-file mypy-adapters.ini | tail -n 1 || true

test-g2-offline: ## G2 end to end on the fixture folder (real parser, scripted models, offline)
	@echo "🧪 Running the offline G2 end-to-end test..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/integration/test_drive_g2_end_to_end.py -q --asyncio-mode=auto -p tests.plugins.network_guard

test-ci: ## Unit suite once: slow tests and the coverage gate (part of `make ci`)
	@echo "🧪 Running the full unit suite (slow tests, coverage)..."
	@$(XDIST_WARNING)
	RUN_SLOW_TESTS=1 DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/unit/ $(PYTEST_UNIT_FLAGS) \
	    --asyncio-mode=auto -m "not timing" $(COVERAGE_FLAGS)
	@echo "🚦 Enforcing per-file coverage thresholds..."
	@$(PYTHON) scripts/check_coverage_thresholds.py
	@echo "🚦 Enforcing per-layer coverage floors..."
	@$(PYTHON) scripts/check_coverage_layers.py

check: lint typecheck test-unit test-g2-offline ## ruff lint + mypy (domain/ports) + unit/architecture tests + offline G2 (CI runs `make ci`)
	@echo "✅ All checks passed"

ci: lint typecheck test-ci test-g2-offline ## PR job: lint + mypy + the unit suite once (slow tests, coverage gate) + offline G2
	@echo "✅ CI passed"

# ============================================================================
# BUILD & DEPLOY
# ============================================================================

deploy: ## Build + deploy to Cloud Run (the single live environment)
	@if [ -z "$(SERVICE_URL_DEV)" ]; then \
		echo "❌ SERVICE_URL_DEV is empty: deploy from a checkout with the real .env (docs/07_deployment/README.md)"; \
		exit 1; fi
	@if [ -z "$(INDEXER_URL_DEV)" ] && [ "$(DRIVE_FIRST_PASS)" != "1" ]; then \
		echo "❌ INDEXER_URL_DEV is empty: drive indexing would be disabled. Set it in .env, or run"; \
		echo "   DRIVE_FIRST_PASS=1 make deploy for the first pass of the two-pass first deploy"; \
		echo "   (docs/07_deployment/README.md § Indexer URL)."; \
		exit 1; fi
	@if [ "$(DRIVE_TRANSCRIBE_ENABLED_DEV)" != "on" ] && [ "$(DRIVE_TRANSCRIBE_ENABLED_DEV)" != "off" ]; then \
		echo "❌ DRIVE_TRANSCRIBE_ENABLED_DEV must be exactly on or off in .env (a missing key would silently freeze in-flight transcription batches): docs/07_deployment/README.md § Transcripts switch and cap"; \
		exit 1; fi
	@echo "🎙  Transcription switch: _DRIVE_TRANSCRIBE_ENABLED=$(_DRIVE_TRANSCRIBE_ENABLED), cap: _DRIVE_TRANSCRIBE_USD_CAP=$(if $(_DRIVE_TRANSCRIBE_USD_CAP),$(_DRIVE_TRANSCRIBE_USD_CAP),(empty = the \$$20 maximum))"
	@echo "📝 Drive notes switch (both services): _DRIVE_NOTES_ENABLED=$(_DRIVE_NOTES_ENABLED)"
	@echo "🚀 Build + deploy to Cloud Run ($(SERVICE_NAME))..."
	gcloud builds submit --config=cloudbuild-dev.yaml \
		--substitutions=_SERVICE_URL=$(SERVICE_URL_DEV),_OAUTH_REDIRECT_URI=$(SERVICE_URL_DEV)/auth/callback,_VOICE_RELAY_URL=$(_VOICE_RELAY_URL),_INDEXER_URL=$(_INDEXER_URL),_DRIVE_UPGRADE_PASS=$(_DRIVE_UPGRADE_PASS),_DRIVE_NOTES_ENABLED=$(_DRIVE_NOTES_ENABLED),_DRIVE_TRANSCRIBE_ENABLED=$(_DRIVE_TRANSCRIBE_ENABLED),_DRIVE_TRANSCRIBE_USD_CAP=$(_DRIVE_TRANSCRIBE_USD_CAP),_SANDBOX_RENDER=$(_SANDBOX_RENDER),_SANDBOX_RENDER_URL=$(_SANDBOX_RENDER_URL),_SANDBOX_RENDER_SA=$(_SANDBOX_RENDER_SA),_SANDBOX_RENDER_CALLERS=$(_SANDBOX_RENDER_CALLERS) .
	@echo "✅ Deployment complete!"

deploy-indexes: ## Deploy Firestore indexes from config/firestore.indexes.json
	@echo "🚀 Deploying Firestore indexes..."
	$(PYTHON) scripts/infrastructure/deploy_firestore_indexes.py config/firestore.indexes.json $(PROJECT_ID)
	@echo "✅ Indexes deployment process completed!"

# ============================================================================
# CLOUD OPERATIONS
# ============================================================================

start: ## Start service (min-instances=1)
	@echo "🟢 Starting $(SERVICE_NAME)..."
	gcloud run services update $(SERVICE_NAME) --min-instances 1 --region $(REGION)
	@echo "✅ Service started (min-instances=1)"

stop: ## Stop service (min-instances=0)
	@echo "🛑 Stopping $(SERVICE_NAME)..."
	gcloud run services update $(SERVICE_NAME) --min-instances 0 --region $(REGION)
	@echo "✅ Service stopped (min-instances=0)"

restart: stop start ## Restart service

services: ## Show service URL
	@echo "🌐 Service URL:"
	@gcloud run services describe $(SERVICE_NAME) --region=$(REGION) --format="value(status.url)" 2>/dev/null || echo "  $(SERVICE_NAME): Not deployed"

# --- Claude PERFORMANCE-tier model kill-switch (Sonnet 5.5 rollout) -----------
# The Claude PERFORMANCE tier (Consolidation + Claude-tier Smart) defaults to
# claude-opus-5-5 in code (ClaudeAdapter.MODEL_TIERS); cloudbuild-dev.yaml no longer pins it
# (pin removed 2026-10-15). These flip the LIVE env var with
# no rebuild/redeploy — the instant rollback lever. `make deploy` drops the live override
# (back to the code default, claude-opus-5-5); for a permanent pin add
# CLAUDE_PERFORMANCE_MODEL to the --set-env-vars in cloudbuild-dev.yaml.
claude-model: ## Show the live CLAUDE_PERFORMANCE_MODEL (unset => code default claude-opus-5-5)
	@gcloud run services describe $(SERVICE_NAME) --region=$(REGION) --format=export 2>/dev/null \
		| grep -A1 "name: CLAUDE_PERFORMANCE_MODEL" || echo "  CLAUDE_PERFORMANCE_MODEL unset => code default claude-opus-5-5"

claude-rollback: ## KILL-SWITCH: flip PERFORMANCE tier live -> Sonnet 5 (no redeploy)
	@echo "⏪ Rolling Claude PERFORMANCE tier back to claude-sonnet-5 (live)..."
	gcloud run services update $(SERVICE_NAME) --region=$(REGION) \
		--update-env-vars CLAUDE_PERFORMANCE_MODEL=claude-sonnet-5
	@echo "✅ Live env flipped. (make deploy drops it: the code default claude-opus-5-5 governs again)"

claude-forward: ## Flip PERFORMANCE tier live -> Sonnet 5.5 (no redeploy)
	@echo "⏩ Rolling Claude PERFORMANCE tier forward to claude-sonnet-5-5 (live)..."
	gcloud run services update $(SERVICE_NAME) --region=$(REGION) \
		--update-env-vars CLAUDE_PERFORMANCE_MODEL=claude-sonnet-5-5
	@echo "✅ Live env flipped."

# --- OpenAI tier kill-switch (GPT-6 rollout, 2026-10) -------------------------
# OPENAI_TIER_OVERRIDES="tier=model,..." remaps OpenAIAdapter.MODEL_TIERS live, no rebuild.
# `make deploy` drops it (not in cloudbuild-dev.yaml) → the GPT-6 tier map in code governs.
openai-model: ## Show the live OPENAI_TIER_OVERRIDES (unset => GPT-6 tier map in code)
	@gcloud run services describe $(SERVICE_NAME) --region=$(REGION) --format=export 2>/dev/null \
		| grep -A1 "name: OPENAI_TIER_OVERRIDES" || echo "  OPENAI_TIER_OVERRIDES unset => tier map in openai_adapter.py"

openai-rollback: ## KILL-SWITCH: OpenAI tiers live -> GPT-5.x map (no redeploy)
	@echo "⏪ Rolling OpenAI tiers back to the GPT-5.x map (live)..."
	gcloud run services update $(SERVICE_NAME) --region=$(REGION) \
		--update-env-vars "^@^OPENAI_TIER_OVERRIDES=eco=gpt-5.4-nano,balanced=gpt-5.6-luna,performance=gpt-5.6-terra,ultra=gpt-5.6-sol"
	@echo "✅ Live env flipped. Run 'make openai-forward' to return to the GPT-6 map."

openai-forward: ## Remove OPENAI_TIER_OVERRIDES -> code tier map governs (live)
	gcloud run services update $(SERVICE_NAME) --region=$(REGION) --remove-env-vars OPENAI_TIER_OVERRIDES
	@echo "✅ Live env flipped."

# Deep Research model levers. NOTE: CLAUDE_DEEP_RESEARCH_MODEL (model_override) pins EVERY
# DR tier, not just PERFORMANCE — it is a coarse break-glass kill-switch. The forward default
# (BALANCED/PERFORMANCE -> Sonnet 5.5, ULTRA -> Opus 5.5, ECO -> Haiku) comes from the adapter's
# MODEL_TIERS map when the override is UNSET; `make dr-forward` restores that by removing it.
dr-model: ## Show the live CLAUDE_DEEP_RESEARCH_MODEL (unset => tier map: Sonnet 5.5 default)
	@gcloud run services describe $(SERVICE_NAME) --region=$(REGION) --format=export 2>/dev/null \
		| grep -A1 "name: CLAUDE_DEEP_RESEARCH_MODEL" || echo "  CLAUDE_DEEP_RESEARCH_MODEL unset => tier map (BALANCED/PERFORMANCE => claude-sonnet-5-5)"

dr-rollback: ## KILL-SWITCH: pin ALL Deep Research tiers live -> Sonnet 5 (no redeploy)
	@echo "⏪ Pinning ALL Deep Research tiers to claude-sonnet-5 (live, coarse kill-switch)..."
	gcloud run services update $(SERVICE_NAME) --region=$(REGION) \
		--update-env-vars CLAUDE_DEEP_RESEARCH_MODEL=claude-sonnet-5
	@echo "✅ Live env flipped. Run 'make dr-forward' to restore the tier map (Sonnet 5.5)."

dr-forward: ## Remove the DR override -> back to tier map (BALANCED/PERFORMANCE = Sonnet 5)
	@echo "⏩ Removing CLAUDE_DEEP_RESEARCH_MODEL override -> tier map governs (live)..."
	gcloud run services update $(SERVICE_NAME) --region=$(REGION) \
		--remove-env-vars CLAUDE_DEEP_RESEARCH_MODEL
	@echo "✅ Override removed. Deep Research now follows MODEL_TIERS (Sonnet 5 default)."

status: ## Show service status
	@echo "📊 Service status ($(SERVICE_NAME)):"
	@gcloud run services describe $(SERVICE_NAME) --region=$(REGION) --format="table(status.conditions[0].type,status.conditions[0].status,metadata.labels)" 2>/dev/null || echo "  Not deployed"

# ============================================================================
# MONITORING & LOGS
# ============================================================================

logs: ## View last K service log entries (default K=300)
	@echo "📋 Logs for $(SERVICE_NAME) (last $(K) entries):"
	@gcloud run services logs read $(SERVICE_NAME) \
	  --region=$(REGION) \
	  --limit=$(K) \
	  --format="value(textPayload)"

logs-tail: ## Live tail logs
	@echo "📡 Tailing $(SERVICE_NAME) logs (Ctrl+C to stop)..."
	@gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME)" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-tail-clean: ## Live tail logs (clean mode)
	@echo "📡 Tailing $(SERVICE_NAME) logs (clean mode, Ctrl+C to stop)..."
	@LOG_TRACE_CONTEXT=clean gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME)" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-tail-full: ## Live tail logs (full mode)
	@echo "📡 Tailing $(SERVICE_NAME) logs (full mode, Ctrl+C to stop)..."
	@LOG_TRACE_CONTEXT=full gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME)" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-perf: ## Live tail perf logs
	@echo "📡 Tailing $(SERVICE_NAME) performance logs (Ctrl+C to stop)..."
	@gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME) AND textPayload=~\"⏱️|✅ END|🧭 PATH|🔧 TOOL\"" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

fetch-logs: ## Fetch last K logs to alek_debug.log (default K=300)
	@echo "📥 Fetching last $(K) log entries to alek_debug.log..."
	@gcloud run services logs read $(SERVICE_NAME) \
	  --region=$(REGION) \
	  --limit=$(K) \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID) \
	  > alek_debug.log
	@echo "✅ Done: $$(wc -l < alek_debug.log) lines written"

logs-mode-clean: ## Set LOG_TRACE_CONTEXT=clean on the service
	@echo "🔧 Setting log mode to clean (LOG_TRACE_CONTEXT=clean)..."
	@gcloud run services update $(SERVICE_NAME) --region=$(REGION) \
	  --update-env-vars LOG_TRACE_CONTEXT=clean
	@echo "✅ Log mode set to clean"

logs-mode-full: ## Set LOG_TRACE_CONTEXT=full on the service
	@echo "🔧 Setting log mode to full (LOG_TRACE_CONTEXT=full)..."
	@gcloud run services update $(SERVICE_NAME) --region=$(REGION) \
	  --update-env-vars LOG_TRACE_CONTEXT=full
	@echo "✅ Log mode set to full"

# --- Async research job (Cloud Run Job: $(RESEARCH_JOB)) ---

logs-job: ## View recent Cloud Run Job logs (last 100 entries)
	@echo "📋 Cloud Run Job logs (last 100 entries):"
	@gcloud run jobs logs read $(RESEARCH_JOB) \
	  --region=$(REGION) \
	  --limit=100 \
	  --project=$(PROJECT_ID)

fetch-logs-job: ## Fetch last K job logs to alek_debug_job.log (default K=300)
	@echo "📥 Fetching last $(K) job log entries to alek_debug_job.log..."
	@gcloud run jobs logs read $(RESEARCH_JOB) \
	  --region=$(REGION) \
	  --limit=$(K) \
	  --project=$(PROJECT_ID) \
	  > alek_debug_job.log
	@echo "✅ Done: $$(wc -l < alek_debug_job.log) lines written"

list-jobs: ## List all executions of the research job with status
	@gcloud run jobs executions list \
	  --job=$(RESEARCH_JOB) \
	  --region=$(REGION) \
	  --project=$(PROJECT_ID)

logs-execution: ## View logs for a specific execution: make logs-execution EXECUTION=<name>
	@test -n "$(EXECUTION)" || (echo "❌ Usage: make logs-execution EXECUTION=<execution-name>" && exit 1)
	@echo "📋 Logs for execution $(EXECUTION):"
	@gcloud logging read \
	  'resource.type=cloud_run_job AND labels."run.googleapis.com/execution_name"=$(EXECUTION)' \
	  --limit=200 \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

cancel-job: ## Cancel a running execution: make cancel-job EXECUTION=<name>
	@test -n "$(EXECUTION)" || (echo "❌ Usage: make cancel-job EXECUTION=<execution-name>" && exit 1)
	@echo "🛑 Cancelling execution $(EXECUTION)..."
	@gcloud run jobs executions cancel $(EXECUTION) \
	  --region=$(REGION) \
	  --project=$(PROJECT_ID)

# --- Voice relay (Cloud Run service: $(VOICE_RELAY)) ---
# A service, not a Job (it holds a live WebSocket per call), so these mirror
# `logs`/`fetch-logs` above (parameterized to VOICE_RELAY) rather than
# `logs-job`/`fetch-logs-job`, which target `gcloud run jobs` — the wrong
# resource type for a second Cloud Run service.

logs-relay: ## View last K voice relay log entries (default K=300)
	@echo "📋 Logs for $(VOICE_RELAY) (last $(K) entries):"
	@gcloud run services logs read $(VOICE_RELAY) \
	  --region=$(REGION) \
	  --limit=$(K) \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

fetch-logs-relay: ## Fetch last K voice relay logs to alek_debug_relay.log (default K=300)
	@echo "📥 Fetching last $(K) voice relay log entries to alek_debug_relay.log..."
	@gcloud run services logs read $(VOICE_RELAY) \
	  --region=$(REGION) \
	  --limit=$(K) \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID) \
	  > alek_debug_relay.log
	@echo "✅ Done: $$(wc -l < alek_debug_relay.log) lines written"

# --- Drive indexer (Cloud Run service: $(INDEXER)) ---
# Same shape as the relay targets above: a second Cloud Run service, not a Job.

logs-indexer: ## View last K drive indexer log entries (default K=300)
	@echo "📋 Logs for $(INDEXER) (last $(K) entries):"
	@gcloud run services logs read $(INDEXER) \
	  --region=$(REGION) \
	  --limit=$(K) \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

fetch-logs-indexer: ## Fetch last K drive indexer logs to alek_debug_indexer.log (default K=300)
	@echo "📥 Fetching last $(K) drive indexer log entries to alek_debug_indexer.log..."
	@gcloud run services logs read $(INDEXER) \
	  --region=$(REGION) \
	  --limit=$(K) \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID) \
	  > alek_debug_indexer.log
	@echo "✅ Done: $$(wc -l < alek_debug_indexer.log) lines written"

# ============================================================================
# MAINTENANCE
# ============================================================================

check-models: ## Check available Gemini models
	@echo "🔍 Checking available models..."
	@$(PYTHON) scripts/validation/check_models.py

check-pricing: ## Fetch live model prices (OpenRouter) and audit billing.py → scripts/memory/pricing_report.md
	@echo "💰 Fetching live model prices and auditing billing.py..."
	@$(PYTHON) scripts/validation/check_pricing.py
	@echo "📄 Open: scripts/memory/pricing_report.md"

# E2E tests (real API flow)

test-e2e-smart: ## E2E: Smart agent
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type smart

test-e2e-quick: ## E2E: Quick agent
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type quick

test-e2e-router: ## E2E: Router agent
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type router

test-e2e-consolidation: ## E2E: Consolidation agent
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type consolidation

test-e2e-websearch: ## E2E: WebSearch agent
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type web_search

test-e2e-all: ## E2E: All agents
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type all

# ============================================================================
# CLEANUP (DANGEROUS)
# ============================================================================

delete: ## Delete the Cloud Run service (DANGEROUS — this is the single live environment)
	@echo "⚠️  WARNING: This will DELETE the live service: $(SERVICE_NAME)"
	@read -p "Type 'DELETE' to confirm: " confirm && [ "$$confirm" = "DELETE" ] || (echo "Aborted" && exit 1)
	@echo "🗑️  Deleting $(SERVICE_NAME)..."
	gcloud run services delete $(SERVICE_NAME) --region=$(REGION) --quiet
	@echo "✅ Service deleted"
