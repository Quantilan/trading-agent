# ── Quantilan Agent — Docker shortcuts ───────────────────────────────────────
#
#  make setup     — first-time init (.env, state file)
#  make gui       — start Setup GUI at http://localhost:8080
#  make start     — start agent in background
#  make stop      — stop agent
#  make restart   — restart agent (picks up .env changes)
#  make logs      — tail agent logs
#  make status    — show running containers
#  make build     — rebuild Docker image
#  make clean     — remove containers and image

COMPOSE = docker compose

.PHONY: setup gui start stop restart logs status build clean

## First-time setup: create .env and state file if missing
setup:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "✅ .env created from .env.example — edit it or use 'make gui'"; \
	else \
		echo "ℹ️  .env already exists"; \
	fi
	@touch agent_state.json
	@mkdir -p logs
	@echo "✅ Ready. Run 'make gui' to configure, then 'make start' to launch agent."

## Open Setup GUI at http://localhost:8080
gui: _ensure_env
	@echo "🌐 Starting Setup GUI at http://localhost:8080 ..."
	$(COMPOSE) up gui

## Start agent in background
start: _ensure_env
	@echo "🚀 Starting agent..."
	$(COMPOSE) up -d agent
	@echo "✅ Agent started. Run 'make logs' to watch."

## Stop agent
stop:
	@echo "🛑 Stopping agent..."
	$(COMPOSE) stop agent

## Restart agent (reload .env)
restart: stop start

## Tail agent logs
logs:
	$(COMPOSE) logs -f --tail=100 agent

## Show status of all containers
status:
	$(COMPOSE) ps

## Rebuild image (after code changes)
build:
	$(COMPOSE) build --no-cache

## Remove containers and image
clean:
	$(COMPOSE) down --rmi local
	@echo "🗑  Containers and image removed."

## Internal: warn if .env missing
_ensure_env:
	@if [ ! -f .env ]; then \
		echo "❌ .env not found. Run 'make setup' first."; \
		exit 1; \
	fi
	@touch agent_state.json
	@mkdir -p logs
