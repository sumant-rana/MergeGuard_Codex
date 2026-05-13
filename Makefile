# MergeGuard — top-level Makefile.
#
# Three deployment modes:
#   • in-process    — `python3 apps/api/main.py` on the host, no Docker
#   • local         — MergeGuard app container + Magenta `--all` agent stack
#                     (both in Docker, talking by service name)
#   • cloud         — MergeGuard app container; agents → Magenta platform
#
# TL;DR:
#   make stack           # bring up the 12-agent Magenta stack
#   make up              # start MergeGuard (local mode) on :4100
#   make logs            # follow MergeGuard container logs
#   make down            # stop MergeGuard
#   make stack-stop      # stop the agent stack
#
#   make up-cloud        # start MergeGuard against Magenta cloud workspaces
#   make logs-cloud
#   make down-cloud

AGENTIC      ?= agentic
LOCAL_COMPOSE := docker-compose.yml
CLOUD_COMPOSE := docker-compose.cloud.yml
ALL_COMPOSE   := .agentic/docker-compose.dev.all.yml

# ── Existing manifest-validation targets ───────────────────────────────

.PHONY: magenta-version magenta-validate magenta-sync-check magenta-check test-agentic

magenta-version:
	$(AGENTIC) version --json

magenta-validate:
	$(AGENTIC) validate agent.yaml
	@for f in agents/*/agent.yaml; do \
		echo "validating $$f"; \
		$(AGENTIC) validate "$$f"; \
	done

magenta-sync-check:
	uv sync --locked --no-dev --package mergeguard-agent-review-compression --dry-run

magenta-check: magenta-validate magenta-sync-check

test-agentic:
	python3 -m unittest discover -s tests_agentic -p '*_test.py'

# ── Local (Docker) mode ────────────────────────────────────────────────
# Brings up the MergeGuard container only; assumes ``make stack`` (or
# ``agentic dev up --all``) is already running for the agent backend.

.PHONY: up down logs rebuild
up:
	@printf "\033[1mStarting MergeGuard (local mode)...\033[0m  agents → docker (oe-<agent>:8000)\n"
	docker compose -f $(LOCAL_COMPOSE) up -d --build
	@printf "\n\033[1;32m✓ Local mode up.\033[0m\n\n"
	@printf "  Dashboard + API:  \033[36mhttp://localhost:4100\033[0m\n"
	@printf "  Agents:           docker, via 'agentic dev up --all'\n"
	@printf "  Mode:             AGENT_MODE=local\n\n"
	@printf "Try it:\n"
	@printf "  \033[1mmake logs\033[0m                            Follow container logs\n"
	@printf "  \033[1mmake down\033[0m                            Stop\n"

down:
	-docker compose -f $(LOCAL_COMPOSE) down
	-docker compose -f $(CLOUD_COMPOSE) down 2>/dev/null
	@printf "\n\033[1;32m✓ MergeGuard stopped.\033[0m\n"

logs:
	docker compose -f $(LOCAL_COMPOSE) logs -f mergeguard

rebuild:
	docker compose -f $(LOCAL_COMPOSE) build --no-cache
	docker compose -f $(LOCAL_COMPOSE) up -d --force-recreate

# ── Cloud mode ─────────────────────────────────────────────────────────
# No Magenta `--all` agent stack needed locally; all invocations stream to
# Magenta cloud workspaces. Required env in repo-root .env:
#   MAGENTA_API_KEY, MAGENTA_PROJECT_ID, and 12 × WORKSPACE_* values
#   (see docker-compose.cloud.yml for the full list).

.PHONY: up-cloud down-cloud logs-cloud rebuild-cloud
up-cloud:
	@printf "\033[1mStarting MergeGuard (cloud-agent mode)...\033[0m  agents → Magenta platform\n"
	docker compose -f $(CLOUD_COMPOSE) up -d --build
	@printf "\n\033[1;32m✓ Cloud mode up.\033[0m\n\n"
	@printf "  Dashboard + API:  \033[36mhttp://localhost:4100\033[0m\n"
	@printf "  Agents:           Magenta platform (workspaces from .env)\n\n"
	@printf "Try it:\n"
	@printf "  \033[1mmake logs-cloud\033[0m                      Follow container logs\n"
	@printf "  \033[1mmake down-cloud\033[0m                      Stop\n"

down-cloud:
	-docker compose -f $(CLOUD_COMPOSE) down
	@printf "\n\033[1;32m✓ Cloud stack stopped.\033[0m\n"

logs-cloud:
	docker compose -f $(CLOUD_COMPOSE) logs -f mergeguard-cloud

rebuild-cloud:
	docker compose -f $(CLOUD_COMPOSE) build --no-cache
	docker compose -f $(CLOUD_COMPOSE) up -d --force-recreate

# ── Magenta agent stack (the 12-agent dev stack) ───────────────────────
# ``agentic dev up --all`` regenerates ``.agentic/docker-compose.dev.all.yml``
# every run with broken memory-server image lines (CMD defaults to /bin/bash
# in runner-base, exits immediately, dependency check fails). We:
#   1. let the CLI generate / refresh the compose (and the agent stack network)
#   2. run scripts/patch-compose.sh which converts each memory-server to
#      `build:` matching the working app-* services
#   3. bring up via docker compose so our patch is preserved

.PHONY: stack stack-stop stack-clean stack-restart logs-stack
stack:
	@printf "  → regenerating $(ALL_COMPOSE) via agentic CLI\n"
	@$(AGENTIC) dev up --all >/dev/null 2>&1 || true
	@docker compose -f $(ALL_COMPOSE) stop >/dev/null 2>&1 || true
	@./scripts/patch-compose.sh
	docker compose -f $(ALL_COMPOSE) up -d --build

stack-stop:
	docker compose -f $(ALL_COMPOSE) stop

stack-clean:
	-docker compose -f $(ALL_COMPOSE) down -v
	-$(AGENTIC) dev clean --all

stack-restart:
	docker compose -f $(ALL_COMPOSE) restart

logs-stack:
	docker compose -f $(ALL_COMPOSE) logs -f
