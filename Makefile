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
.PHONY: help setup seed seed-empty run docker-build docker-up docker-down docker-clean docker-seed docker-seed-empty docker-test docker-logs api ui test test-inventory verify-clone outcome-schema eval eval-fresh eval-repeat eval-source llm-smoke clean distclean check-env cache-clear

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

check-env: ## Report configuration and whether each provider is reachable
	@$(PY) scripts/check_env.py

llm-smoke: ## Send one real chunk to the configured model and show the result
	$(PY) scripts/llm_smoke.py $(if $(PROVIDER),--provider $(PROVIDER),)

cache-clear: ## Drop the cached model responses
	@$(PY) -c "import sys; sys.path.insert(0,'backend'); \
from app.config import get_settings; from app.extraction.llm.cache import ResponseCache; \
s=get_settings(); print(f'removed {ResponseCache(s.llm_cache_dir).clear()} cached response(s)')"

seed: ## Rebuild the database from schema.sql and load sample data
	$(PY) scripts/seed.py

seed-empty: ## Rebuild the database with no sources, ready for your own data
	$(PY) scripts/seed.py --empty

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

outcome-schema: ## Regenerate docs/outcome_schema.json from the contract
	$(PY) scripts/emit_outcome_schema.py

verify-clone: ## Clone this branch to a temp dir and run the suite there
	$(PY) scripts/verify_clone.py

test-inventory: ## Tests per file, so docs/testing.md cannot silently drift
	@$(PYTEST) --collect-only -q 2>/dev/null | grep ': [0-9]' \
		| sed 's|backend/tests/||;s|eval/||' \
		| awk -F': ' '{printf "  %-38s %3d\n", $$1, $$2; total += $$2} END {printf "  %-38s %3d\n", "TOTAL", total}'

eval: ## Run the golden test cases and write eval/results.txt
	$(PY) eval/harness.py

eval-fresh: ## Same, bypassing the response cache, to prove the numbers reproduce
	$(PY) eval/harness.py --no-cache

eval-repeat: ## Three uncached runs, reported as a range (the model is not deterministic)
	$(PY) eval/harness.py --runs $(or $(RUNS),3)

eval-source: ## Score one source against its golden labels: make eval-source SOURCE=<id>
	@test -n "$(SOURCE)" || { printf "usage: make eval-source SOURCE=<source_id>\n"; exit 1; }
	$(PY) eval/harness.py --sources $(SOURCE)

# --- Docker ------------------------------------------------------------------
# The reproducibility claim: these five targets are the whole contract. A
# reviewer with Docker and a key needs nothing else installed, no Python
# version, no Node, no model weights.

docker-build: ## Build both images
	docker compose build

docker-up: ## Build if needed and run the whole thing
	@if [ ! -f .env ]; then cp .env.example .env; printf "created .env from .env.example, add your key\n"; fi
	docker compose up --build -d
	@printf "\n$(BOLD)interface$(OFF) http://localhost:5173    $(BOLD)api$(OFF) http://localhost:8000/health\n"
	@printf "$(DIM)next: make docker-seed, or upload your own on the Sources tab$(OFF)\n"

docker-down: ## Stop everything, keeping the store
	docker compose down

docker-clean: ## Stop everything and delete the store, the logs and the model cache
	docker compose down --volumes

docker-seed: ## Load the committed sample data into the running container
	docker compose exec api python scripts/seed.py

docker-seed-empty: ## Schema and the tracker backlog only, ready for your own data
	docker compose exec api python scripts/seed.py --empty

docker-test: ## Run the whole suite inside the image, which is the real check
	docker compose run --rm --no-deps api python -m pytest -q

docker-logs: ## Follow both services
	docker compose logs -f


clean: ## Remove the database, indexes and generated artefacts
	rm -rf data/*.db data/*.db-wal data/*.db-shm data/faiss data/llm_cache \
	       data/documents data/uploads data/digests data/outcome_records \
	       write_log/*.jsonl
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
	@printf "cleaned\n"

distclean: clean ## Also remove the virtualenv, node_modules and model weights
	rm -rf $(VENV) $(FRONTEND_DIR)/node_modules models .cache
	@printf "removed virtualenv, node_modules and cached model weights\n"
