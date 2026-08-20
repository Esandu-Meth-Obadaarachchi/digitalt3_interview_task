# =============================================================================
# Meeting & Channel Intelligence Agent
# =============================================================================
# Three commands take a clean clone to a running system:
#
#   make setup    create the virtualenv and install dependencies
#   make seed     rebuild the store from schema.sql and load sample data
#   make run      start the API (and the review UI once it exists)
#
# =============================================================================

BASE_PYTHON ?= python3.11
VENV        := .venv
PY          := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
PYTEST      := $(VENV)/bin/pytest
UVICORN     := $(VENV)/bin/uvicorn

FRONTEND_DIR := frontend

BOLD := \033[1m
DIM  := \033[2m
OFF  := \033[0m

.DEFAULT_GOAL := help
.PHONY: help setup seed run api ui test eval lint clean distclean check-env

help: ## Show this help
	@printf "\n$(BOLD)Meeting & Channel Intelligence Agent$(OFF)\n\n"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  $(BOLD)%-12s$(OFF) %s\n", $$1, $$2}'
	@printf "\n$(DIM)First run: cp .env.example .env, add GEMINI_API_KEY, then make setup seed run$(OFF)\n\n"

$(VENV)/bin/python:
	@printf "$(DIM)creating virtualenv with $(BASE_PYTHON)$(OFF)\n"
	@command -v $(BASE_PYTHON) >/dev/null 2>&1 || { \
		printf "$(BOLD)$(BASE_PYTHON) not found.$(OFF) Python 3.11 is required: it is the newest\n"; \
		printf "version with settled wheels for faiss-cpu, sentence-transformers and\n"; \
		printf "ctranslate2 on Apple Silicon. Override with: make setup BASE_PYTHON=/path/to/python3.11\n"; \
		exit 1; }
	$(BASE_PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

setup: $(VENV)/bin/python ## Install backend and frontend dependencies
	$(PIP) install --quiet -r backend/requirements.txt
	@printf "backend dependencies installed\n"
	@if [ -f $(FRONTEND_DIR)/package.json ]; then \
		printf "$(DIM)installing frontend dependencies$(OFF)\n"; \
		cd $(FRONTEND_DIR) && npm install --silent; \
	else \
		printf "$(DIM)frontend not present yet, skipping npm install$(OFF)\n"; \
	fi
	@if [ ! -f .env ]; then cp .env.example .env; printf "created .env from .env.example, add your key\n"; fi
	@printf "\nsetup complete. Next: $(BOLD)make seed$(OFF)\n"

check-env: ## Report which configuration the system will run with
	@$(PY) -c "import sys; sys.path.insert(0,'backend'); from app.config import settings as s; \
print(f'provider={s.llm_provider} model={s.gemini_model if s.llm_provider==\"gemini\" else s.ollama_model}'); \
print(f'key_present={bool(s.gemini_api_key)} retrieval={s.retrieval_mode} tracker={s.tracker_provider}'); \
print(f'db={s.db_path}')"

seed: ## Rebuild the database from schema.sql and load sample data
	$(PY) scripts/seed.py

run: ## Start the API server
	$(UVICORN) app.main:app --app-dir backend --host $${API_HOST:-127.0.0.1} --port $${API_PORT:-8000} --reload

api: run ## Alias for run

ui: ## Start the React review interface
	@if [ -f $(FRONTEND_DIR)/package.json ]; then \
		cd $(FRONTEND_DIR) && npm run dev; \
	else \
		printf "frontend not built yet (arrives in Phase 3)\n"; exit 1; \
	fi

test: ## Run the test suite
	$(PYTEST)

eval: ## Run the golden test cases and write eval/results.txt
	@if [ -f eval/harness.py ]; then $(PY) eval/harness.py; \
	else printf "eval harness not built yet (arrives in Phase 3)\n"; exit 1; fi

clean: ## Remove the database, indexes and generated artefacts
	rm -rf data/*.db data/*.db-wal data/*.db-shm data/faiss data/llm_cache \
	       data/digests data/outcome_records write_log/*.jsonl
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
	@printf "cleaned\n"

distclean: clean ## Also remove the virtualenv, node_modules and model weights
	rm -rf $(VENV) $(FRONTEND_DIR)/node_modules models .cache
	@printf "removed virtualenv, node_modules and cached model weights\n"
