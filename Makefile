# Alek-Core Makefile
# Best Practices: https://makefiletutorial.com/

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

# Service Configuration
SERVICE_NAME ?= alek-bot
SERVICE_NAME_DEV ?= alek-bot-dev

# Cloud Run Service URLs — defined in .env (gitignored), loaded via include above
# Required: SERVICE_URL_DEV, SERVICE_URL_PROD
OAUTH_REDIRECT_URI_DEV ?= $(SERVICE_URL_DEV)/auth/callback
OAUTH_REDIRECT_URI_PROD ?= $(SERVICE_URL_PROD)/auth/callback

# Prompt inspection helpers (fallback order: DEV/PROD-specific → USER_ID)
DEV_USER_ID ?= $(USER_ID)
PROD_USER_ID ?= $(USER_ID)


# ============================================================================
# PHONY TARGETS (targets that don't create files)
# ============================================================================

.PHONY: help
.PHONY: install install-dev clean
.PHONY: dev dev-emulator run test lint format kill-local
.PHONY: deploy deploy-dev deploy-indexes
.PHONY: start stop restart start-dev stop-dev
.PHONY: logs logs-dev logs-tail logs-perf logs-dev-tail
.PHONY: services status auth
.PHONY: check-models
.PHONY: test-e2e-smart test-e2e-quick test-e2e-router test-e2e-consolidation test-e2e-websearch test-e2e-all
.PHONY: check
.PHONY: delete-prod delete-dev delete-all

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
	@echo "  make install         Install production dependencies"
	@echo "  make install-dev     Install dev dependencies (includes testing tools)"
	@echo "  make clean           Clean up temporary files and caches"
	@echo "  make auth            Authenticate with Google Cloud"
	@echo ""
	@echo "🔧 DEVELOPMENT:"
	@echo "  make dev             Run bot locally (Socket Mode)"
	@echo "  make dev-emulator    Run with Firestore emulator (port 8081)"
	@echo "  make kill-local      Kill all local bot processes"
	@echo "  make test            Run all tests"
	@echo "  make test-unit       Run unit tests"
	@echo "  make test-integration Run integration tests"
	@echo "  make lint            Run code linting"
	@echo "  make format          Format code"
	@echo ""
	@echo "🏗️  BUILD & DEPLOY:"
	@echo "  make deploy          Build + deploy to Cloud Run (production)"
	@echo "  make deploy-dev      Build + deploy to Cloud Run (development)"
	@echo ""
	@echo "⚙️  CLOUD OPERATIONS:"
	@echo "  make start           Start production service (min-instances=1)"
	@echo "  make stop            Stop production service (min-instances=0)"
	@echo "  make restart         Restart production service"
	@echo "  make start-dev       Start development service"
	@echo "  make stop-dev        Stop development service"
	@echo ""
	@echo "📊 MONITORING & LOGS:"
	@echo "  make logs            View production logs (last 30 entries)"
	@echo "  make logs-dev        View development logs (last 150 entries)"
	@echo "  make logs-tail       Live tail production logs"
	@echo "  make logs-perf       Live tail production perf logs"
	@echo "  make logs-dev-tail   Live tail development logs"
	@echo "  make logs-tail-clean      Tail prod logs (clean mode)"
	@echo "  make logs-tail-full       Tail prod logs (full mode)"
	@echo "  make logs-dev-tail-clean  Tail dev logs (clean mode)"
	@echo "  make logs-dev-tail-full   Tail dev logs (full mode)"
	@echo "  make logs-mode-dev-clean  Set LOG_TRACE_CONTEXT=clean in dev service"
	@echo "  make logs-mode-dev-full   Set LOG_TRACE_CONTEXT=full in dev service"
	@echo "  make logs-mode-prod-clean Set LOG_TRACE_CONTEXT=clean in prod service"
	@echo "  make logs-mode-prod-full  Set LOG_TRACE_CONTEXT=full in prod service"
	@echo "  make services        Show all service URLs"
	@echo "  make status          Show service status"
	@echo ""
	@echo "🗄️  MAINTENANCE:"
	@echo "  make check-models    Check available Gemini models"
	@echo ""
	@echo "🧪 E2E TESTING (Production Flow):"
	@echo "  make test-e2e-smart          E2E test Smart agent"
	@echo "  make test-e2e-quick          E2E test Quick agent"
	@echo "  make test-e2e-router         E2E test Router agent"
	@echo "  make test-e2e-consolidation  E2E test Consolidation agent"
	@echo "  make test-e2e-websearch      E2E test WebSearch agent"
	@echo "  make test-e2e-all            E2E test all agents"
	@echo ""
	@echo "🗑️  CLEANUP:"
	@echo "  make delete-prod        Delete production service (DANGEROUS)"
	@echo "  make delete-dev         Delete development service"
	@echo "  make delete-all         Delete ALL services (prod + dev)"
	@echo ""
	@echo "💡 Tip: Use 'make <target>' to run any command"
	@echo ""

# ============================================================================
# INSTALLATION & SETUP
# ============================================================================

install: ## Install production dependencies
	@echo "📦 Installing production dependencies..."
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

install-dev: install ## Install development dependencies
	@echo "📦 Installing development dependencies..."
	$(PYTHON) -m pip install pytest pytest-asyncio pytest-mock black flake8 mypy

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
# DEVELOPMENT
# ============================================================================

dev: ## Run bot locally (Socket Mode)
	@echo "🏃 Starting bot in local development mode..."
	@if [ -d "venv" ]; then \
		echo "🐍 Using venv"; \
		. venv/bin/activate && export APP_ENV=development SLACK_MODE=socket && python3 main.py; \
	else \
		export APP_ENV=development SLACK_MODE=socket && $(PYTHON) main.py; \
	fi

run: dev ## Alias for 'make dev'

kill-local: ## Kill all local bot processes
	@echo "🔪 Killing all local bot processes..."
	@pkill -9 -f "main.py" 2>/dev/null && echo "✅ All bot processes killed" || echo "ℹ️  No bot processes found"

dev-emulator: ## Run with Firestore emulator (emulator on port 8081, app on 8080)
	@echo "🏠 Starting bot with Firestore emulator..."
	@export FIRESTORE_EMULATOR_HOST=localhost:8081 APP_ENV=development SLACK_MODE=socket && $(PYTHON) main.py

test: ## Run all tests
	@echo "🧪 Running all tests..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/ -v --asyncio-mode=auto

test-unit: ## Run unit tests
	@echo "🧪 Running unit tests..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/unit/ -v --asyncio-mode=auto

test-integration: ## Run integration tests
	@echo "🧪 Running integration tests..."
	DEBUG_PROMPTS=false $(PYTHON) -m pytest tests/integration/ -v --asyncio-mode=auto

lint: ## Run code linting
	@echo "🔍 Running linter..."
	@echo "⚠️  Linting not yet configured (Milestone 4)"
	@echo "Planned: flake8 src/ --max-line-length=120"

format: ## Format code with black
	@echo "✨ Formatting code..."
	@echo "⚠️  Formatter not yet configured (Milestone 4)"
	@echo "Planned: black src/ --line-length=120"

check: test-unit ## Quick pre-commit check (unit tests + domain purity)
	@echo "🔍 Checking domain purity (no infrastructure imports)..."
	@if grep -rn "from src\.adapters\|from src\.config\|from src\.utils\|from src\.infrastructure" src/domain/ --include="*.py" 2>/dev/null | grep -v "__pycache__"; then \
		echo "❌ Domain purity violation found!"; \
		exit 1; \
	else \
		echo "✅ Domain is clean"; \
	fi
	@echo "✅ All checks passed"

# ============================================================================
# BUILD & DEPLOY
# ============================================================================

deploy: ## Build + deploy to Cloud Run (production)
	@echo "🚀 Build + deploy to Cloud Run (PRODUCTION)..."
	@echo "Using cloudbuild-prod.yaml configuration"
	gcloud builds submit --config=cloudbuild-prod.yaml \
		--substitutions=_SERVICE_URL=$(SERVICE_URL_PROD),_OAUTH_REDIRECT_URI=$(OAUTH_REDIRECT_URI_PROD) .
	@echo "✅ Production deployment complete!"

deploy-dev: ## Build + deploy to Cloud Run (development)
	@echo "🚀 Build + deploy to Cloud Run (DEVELOPMENT)..."
	@echo "Using cloudbuild-dev.yaml configuration"
	gcloud builds submit --config=cloudbuild-dev.yaml \
		--substitutions=_SERVICE_URL=$(SERVICE_URL_DEV),_OAUTH_REDIRECT_URI=$(OAUTH_REDIRECT_URI_DEV) .
	@echo "✅ Development deployment complete!"

deploy-indexes: ## Deploy Firestore indexes from firestore.indexes.json
	@echo "🚀 Deploying Firestore indexes..."
	$(PYTHON) scripts/infrastructure/deploy_firestore_indexes.py firestore.indexes.json $(PROJECT_ID)
	@echo "✅ Indexes deployment process completed!"

# ============================================================================
# CLOUD OPERATIONS
# ============================================================================

start: ## Start production service
	@echo "🟢 Starting production service..."
	gcloud run services update $(SERVICE_NAME) --min-instances 1 --region $(REGION)
	@echo "✅ Service started (min-instances=1)"

stop: ## Stop production service
	@echo "🛑 Stopping production service..."
	gcloud run services update $(SERVICE_NAME) --min-instances 0 --region $(REGION)
	@echo "✅ Service stopped (min-instances=0)"

restart: stop start ## Restart production service

start-dev: ## Start development service
	@echo "🟢 Starting development service..."
	gcloud run services update $(SERVICE_NAME_DEV) --min-instances 1 --region $(REGION)
	@echo "✅ Development service started"

stop-dev: ## Stop development service
	@echo "🛑 Stopping development service..."
	gcloud run services update $(SERVICE_NAME_DEV) --min-instances 0 --region $(REGION)
	@echo "✅ Development service stopped"

# ============================================================================
# MONITORING & LOGS
# ============================================================================

logs: ## View production logs (last 30 entries)
	@echo "📋 Production logs (last 30 entries):"
	@gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME)" \
	  --limit 30 \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-dev: ## View development logs (last 150 entries)
	@echo "📋 Development logs (last 150 entries):"
	@gcloud run services logs read $(SERVICE_NAME_DEV) \
	  --region=$(REGION) \
	  --limit=150 \
	  --format="value(textPayload)"

logs-tail: ## Live tail production logs
	@echo "📡 Tailing production logs (Ctrl+C to stop)..."
	@gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME)" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-tail-clean: ## Live tail production logs (clean mode)
	@echo "📡 Tailing production logs (clean mode, Ctrl+C to stop)..."
	@LOG_TRACE_CONTEXT=clean gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME)" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-tail-full: ## Live tail production logs (full mode)
	@echo "📡 Tailing production logs (full mode, Ctrl+C to stop)..."
	@LOG_TRACE_CONTEXT=full gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME)" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-perf: ## Live tail production perf logs
	@echo "📡 Tailing production performance logs (Ctrl+C to stop)..."
	@gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME) AND textPayload=~\"⏱️|✅ END|🧭 PATH|🔧 TOOL\"" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-dev-tail: ## Live tail development logs
	@echo "📡 Tailing development logs (Ctrl+C to stop)..."
	@gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME_DEV)" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-dev-tail-clean: ## Live tail development logs (clean mode)
	@echo "📡 Tailing development logs (clean mode, Ctrl+C to stop)..."
	@LOG_TRACE_CONTEXT=clean gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME_DEV)" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-dev-tail-full: ## Live tail development logs (full mode)
	@echo "📡 Tailing development logs (full mode, Ctrl+C to stop)..."
	@LOG_TRACE_CONTEXT=full gcloud beta logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$(SERVICE_NAME_DEV)" \
	  --format="value(textPayload)" \
	  --project=$(PROJECT_ID)

logs-mode-dev-clean: ## Set LOG_TRACE_CONTEXT=clean in dev service
	@echo "🔧 Setting dev log mode to clean (LOG_TRACE_CONTEXT=clean)..."
	@gcloud run services update $(SERVICE_NAME_DEV) --region=$(REGION) \
	  --update-env-vars LOG_TRACE_CONTEXT=clean
	@echo "✅ Dev log mode set to clean"

logs-mode-dev-full: ## Set LOG_TRACE_CONTEXT=full in dev service
	@echo "🔧 Setting dev log mode to full (LOG_TRACE_CONTEXT=full)..."
	@gcloud run services update $(SERVICE_NAME_DEV) --region=$(REGION) \
	  --update-env-vars LOG_TRACE_CONTEXT=full
	@echo "✅ Dev log mode set to full"

logs-mode-prod-clean: ## Set LOG_TRACE_CONTEXT=clean in prod service
	@echo "🔧 Setting prod log mode to clean (LOG_TRACE_CONTEXT=clean)..."
	@gcloud run services update $(SERVICE_NAME) --region=$(REGION) \
	  --update-env-vars LOG_TRACE_CONTEXT=clean
	@echo "✅ Prod log mode set to clean"

logs-mode-prod-full: ## Set LOG_TRACE_CONTEXT=full in prod service
	@echo "🔧 Setting prod log mode to full (LOG_TRACE_CONTEXT=full)..."
	@gcloud run services update $(SERVICE_NAME) --region=$(REGION) \
	  --update-env-vars LOG_TRACE_CONTEXT=full
	@echo "✅ Prod log mode set to full"

services: ## Show all service URLs
	@echo "🌐 Service URLs:"
	@echo ""
	@echo "Production:"
	@gcloud run services describe $(SERVICE_NAME) --region=$(REGION) --format="value(status.url)" 2>/dev/null || echo "  $(SERVICE_NAME): Not deployed"
	@echo ""
	@echo "Development:"
	@gcloud run services describe $(SERVICE_NAME_DEV) --region=$(REGION) --format="value(status.url)" 2>/dev/null || echo "  $(SERVICE_NAME_DEV): Not deployed"
	@echo ""

status: ## Show service status
	@echo "📊 Service Status:"
	@echo ""
	@echo "Production ($(SERVICE_NAME)):"
	@gcloud run services describe $(SERVICE_NAME) --region=$(REGION) --format="table(status.conditions[0].type,status.conditions[0].status,metadata.labels)" 2>/dev/null || echo "  Not deployed"
	@echo ""
	@echo "Development ($(SERVICE_NAME_DEV)):"
	@gcloud run services describe $(SERVICE_NAME_DEV) --region=$(REGION) --format="table(status.conditions[0].type,status.conditions[0].status,metadata.labels)" 2>/dev/null || echo "  Not deployed"

# ============================================================================
# MAINTENANCE
# ============================================================================

check-models: ## Check available Gemini models
	@echo "🔍 Checking available models..."
	@$(PYTHON) scripts/validation/check_models.py

# E2E tests (production flow)

test-e2e-smart: ## E2E: Smart agent (production flow)
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type smart

test-e2e-quick: ## E2E: Quick agent (production flow)
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type quick

test-e2e-router: ## E2E: Router agent (production flow)
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type router

test-e2e-consolidation: ## E2E: Consolidation agent
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type consolidation

test-e2e-websearch: ## E2E: WebSearch agent (production flow)
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type web_search

test-e2e-all: ## E2E: All agents (production flow)
	@APP_ENV=development $(PYTHON) scripts/prompt/test_agent_e2e.py --agent-type all

# ============================================================================
# CLEANUP (DANGEROUS)
# ============================================================================

delete-prod: ## Delete production service (DANGEROUS)
	@echo "⚠️  WARNING: This will DELETE the production service!"
	@echo "Service: $(SERVICE_NAME)"
	@read -p "Type 'DELETE' to confirm: " confirm && [ "$$confirm" = "DELETE" ] || (echo "Aborted" && exit 1)
	@echo "🗑️  Deleting production service..."
	gcloud run services delete $(SERVICE_NAME) --region=$(REGION) --quiet
	@echo "✅ Production service deleted"

delete-dev: ## Delete development service
	@echo "🗑️  Deleting development service..."
	gcloud run services delete $(SERVICE_NAME_DEV) --region=$(REGION) --quiet
	@echo "✅ Development service deleted"

delete-all: ## Delete ALL services (DANGEROUS)
	@echo "⚠️  WARNING: This will DELETE ALL services (production + development)!"
	@read -p "Type 'DELETE ALL' to confirm: " confirm && [ "$$confirm" = "DELETE ALL" ] || (echo "Aborted" && exit 1)
	@echo "🗑️  Deleting all services..."
	@gcloud run services delete $(SERVICE_NAME) --region=$(REGION) --quiet 2>/dev/null || echo "Production service not found"
	@gcloud run services delete $(SERVICE_NAME_DEV) --region=$(REGION) --quiet 2>/dev/null || echo "Development service not found"
	@echo "✅ All services deleted"
