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

# The single live Cloud Run service + its async research job.
SERVICE_NAME ?= alek-bot-dev
RESEARCH_JOB ?= alek-research-job-dev

# Cloud Run service URL — defined in .env (gitignored), loaded via include above.
# Required: SERVICE_URL_DEV. The deploy-time OAuth callback is derived from it in
# the deploy target's substitutions — NOT from the local OAUTH_REDIRECT_URI in
# .env (that one is a localhost value, used only for running the app locally).

# Default entry count for log reads
K ?= 300

# ============================================================================
# PHONY TARGETS (targets that don't create files)
# ============================================================================

.PHONY: help
.PHONY: install install-dev clean auth
.PHONY: test test-unit test-coverage test-integration lint format check
.PHONY: deploy deploy-indexes
.PHONY: start stop restart
.PHONY: logs logs-tail logs-tail-clean logs-tail-full logs-perf fetch-logs
.PHONY: logs-mode-clean logs-mode-full
.PHONY: logs-job fetch-logs-job list-jobs logs-execution cancel-job
.PHONY: services status
.PHONY: check-models check-pricing
.PHONY: test-e2e-smart test-e2e-quick test-e2e-router test-e2e-consolidation test-e2e-websearch test-e2e-all
.PHONY: create-debug-bucket
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
	@echo "  make check           CI gate: ruff lint + unit/architecture tests"
	@echo "  make test            Run all tests"
	@echo "  make test-unit       Run unit tests"
	@echo "  make test-coverage   Unit tests + per-file coverage gate"
	@echo "  make test-integration Run integration tests"
	@echo "  make lint            Lint src/ with ruff"
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
	$(PYTHON) -m pip install pytest pytest-asyncio pytest-mock ruff

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

test: ## Run all tests
	@echo "🧪 Running all tests..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/ -v --asyncio-mode=auto

test-unit: ## Run unit tests
	@echo "🧪 Running unit tests..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/unit/ -v --asyncio-mode=auto

test-coverage: ## Unit tests + global + per-file coverage gate (RFC § 8.4)
	@echo "🧪 Running unit tests with coverage..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/unit/ \
	    --asyncio-mode=auto \
	    --cov=src \
	    --cov-report=term-missing:skip-covered \
	    --cov-report=json \
	    --cov-fail-under=70
	@echo "🚦 Enforcing per-file coverage thresholds..."
	@$(PYTHON) scripts/check_coverage_thresholds.py

test-integration: ## Run integration tests
	@echo "🧪 Running integration tests..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/integration/ -v --asyncio-mode=auto

lint: ## Lint src/ with ruff (pyflakes + pycodestyle errors)
	@echo "🔍 Running ruff linter on src/..."
	$(PYTHON) -m ruff check src/

format: ## Format src/ with ruff (black-compatible)
	@echo "✨ Formatting src/ with ruff..."
	$(PYTHON) -m ruff format src/

check: lint test-unit ## CI gate: ruff lint + unit/architecture tests
	@echo "✅ All checks passed"

# ============================================================================
# BUILD & DEPLOY
# ============================================================================

deploy: ## Build + deploy to Cloud Run (the single live environment)
	@echo "🚀 Build + deploy to Cloud Run ($(SERVICE_NAME))..."
	gcloud builds submit --config=cloudbuild-dev.yaml \
		--substitutions=_SERVICE_URL=$(SERVICE_URL_DEV),_OAUTH_REDIRECT_URI=$(SERVICE_URL_DEV)/auth/callback,_DEBUG_PROMPTS_BUCKET=$(DEBUG_PROMPTS_BUCKET) .
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
# DEBUG INFRASTRUCTURE
# ============================================================================

create-debug-bucket: ## Create private GCS bucket for agent prompt/response debug logs
	@[ -n "$(DEBUG_PROMPTS_BUCKET)" ] || (echo "❌ DEBUG_PROMPTS_BUCKET is not set in .env" && exit 1)
	@echo "🪣 Creating debug bucket: $(DEBUG_PROMPTS_BUCKET)"
	gcloud storage buckets create gs://$(DEBUG_PROMPTS_BUCKET) \
		--project=$(PROJECT_ID) \
		--location=$(REGION) \
		--uniform-bucket-level-access \
		--no-public-access-prevention
	@echo "🔒 Granting Cloud Run SA write access..."
	$(eval PROJECT_NUMBER := $(shell gcloud projects describe $(PROJECT_ID) --format="value(projectNumber)"))
	gcloud storage buckets add-iam-policy-binding gs://$(DEBUG_PROMPTS_BUCKET) \
		--member="serviceAccount:$(PROJECT_NUMBER)-compute@developer.gserviceaccount.com" \
		--role="roles/storage.objectCreator"
	@echo "✅ Bucket ready. Add to .env: DEBUG_PROMPTS_BUCKET=$(DEBUG_PROMPTS_BUCKET)"
	@echo "   Add to Cloud Build trigger substitutions: _DEBUG_PROMPTS_BUCKET=$(DEBUG_PROMPTS_BUCKET)"

# ============================================================================
# CLEANUP (DANGEROUS)
# ============================================================================

delete: ## Delete the Cloud Run service (DANGEROUS — this is the single live environment)
	@echo "⚠️  WARNING: This will DELETE the live service: $(SERVICE_NAME)"
	@read -p "Type 'DELETE' to confirm: " confirm && [ "$$confirm" = "DELETE" ] || (echo "Aborted" && exit 1)
	@echo "🗑️  Deleting $(SERVICE_NAME)..."
	gcloud run services delete $(SERVICE_NAME) --region=$(REGION) --quiet
	@echo "✅ Service deleted"
