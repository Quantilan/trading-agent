# ── Quantilan Agent — Docker shortcuts ───────────────────────────────────────
#
#  make setup     — first-time init (.env, state file)
#  make gui       — start Setup GUI at http://localhost:8080
#  make start     — start agent in background
#  make stop      — stop agent
#  make restart   — restart agent (picks up .env changes)
#  make logs      — tail agent logs
#  make status    — show running containers
#  make build     — rebuild Docker image (with cache, fast)
#  make rebuild   — rebuild from scratch (after requirements.txt changes)
#  make clean     — remove containers and image

# Auto-detect: use sudo docker compose if current user can't reach Docker socket
COMPOSE = $(shell docker info >/dev/null 2>&1 && echo "docker compose" || echo "sudo docker compose")

.PHONY: setup gui start stop restart logs status build clean

## First-time setup: create .env and state file if missing
setup:
	@if [ -d .env ]; then \
		echo "⚠️  .env is a directory (Docker artifact) — removing and recreating as file..."; \
		rm -rf .env; \
	fi
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

## Rebuild image using cache (fast, for routine updates)
build:
	$(COMPOSE) build

## Rebuild image from scratch, no cache (use after requirements.txt changes)
rebuild:
	$(COMPOSE) build --no-cache

## Print SSH tunnel command (run this on your LOCAL machine to access GUI on VPS)
tunnel:
	@VPS_IP=$$(curl -s --max-time 3 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $$1}'); \
	echo ""; \
	echo "  Run this on your LOCAL machine:"; \
	echo ""; \
	echo "    ssh -L 8080:localhost:8080 $$(whoami)@$$VPS_IP"; \
	echo ""; \
	echo "  Then open: http://localhost:8080"; \
	echo ""

## Remove containers and image
clean:
	$(COMPOSE) down --rmi local
	@echo "🗑  Containers and image removed."

## Internal: ensure .env is a file (not a dir) and exists
_ensure_env:
	@if [ -d .env ]; then \
		echo "⚠️  .env is a directory (Docker artifact) — fixing..."; \
		rm -rf .env; \
		cp .env.example .env; \
		echo "✅ .env re-created from .env.example — configure it via 'make gui'"; \
	fi
	@if [ ! -f .env ]; then \
		echo "❌ .env not found. Run 'make setup' first."; \
		exit 1; \
	fi
	@touch agent_state.json
	@mkdir -p logs
