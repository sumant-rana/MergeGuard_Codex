AGENTIC ?= ./tools/bin/agentic

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
