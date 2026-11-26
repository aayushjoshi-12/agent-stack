.PHONY: help install install-tool lint fmt typecheck test check run clean \
        plan handoff smoke tree

.DEFAULT_GOAL := help

UV ?= uv
SRC_DIRS := src tests
PY_FIND := find src tests -name '*.py' 2>/dev/null

PHASE ?= P00
TITLE ?= scratch

# ── Help ──────────────────────────────────────────────────────────────────────

# A target with a trailing `## description` shows up here.
help: ## show this list
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ── Install ────────────────────────────────────────────────────────────────────

install: ## install the project and dev dependencies (uv sync)
	$(UV) sync

# Put `agent-stack` on PATH (editable, so source changes are picked up).
install-tool: ## put agent-stack on PATH (editable)
	$(UV) tool install --editable .

# ── Quality gates ──────────────────────────────────────────────────────────────

lint: ## ruff check, and fail on unformatted code (make fmt fixes it)
	@if [ -n "$$($(PY_FIND))" ]; then \
		$(UV) run ruff check $(SRC_DIRS) && $(UV) run ruff format --check $(SRC_DIRS); \
	else \
		echo "lint: no .py files yet, skipping"; \
	fi

fmt: ## sort imports and format with ruff
	@if [ -n "$$($(PY_FIND))" ]; then \
		$(UV) run ruff check --select I --fix $(SRC_DIRS); \
		$(UV) run ruff format $(SRC_DIRS); \
	else \
		echo "fmt: no .py files yet, skipping"; \
	fi

typecheck: ## strict mypy on src
	@if [ -n "$$(find src -name '*.py' 2>/dev/null)" ]; then \
		$(UV) run mypy src; \
	else \
		echo "typecheck: no .py files yet, skipping"; \
	fi

test: ## run the test suite
	@$(UV) run pytest -q tests; code=$$?; \
	if [ $$code -eq 5 ]; then echo "test: no tests collected yet"; exit 0; \
	else exit $$code; fi

# The commit gate: lint + typecheck + test, run concurrently.
check: ## the commit gate: lint (rules + formatting) + typecheck + test, concurrently
	@($(MAKE) --no-print-directory lint) & P1=$$!; \
	 ($(MAKE) --no-print-directory typecheck) & P2=$$!; \
	 ($(MAKE) --no-print-directory test) & P3=$$!; \
	 wait $$P1; R1=$$?; wait $$P2; R2=$$?; wait $$P3; R3=$$?; \
	 exit $$((R1 | R2 | R3))

# ── Run ───────────────────────────────────────────────────────────────────────

run: ## start the app (python -m agent_stack)
	$(UV) run python -m agent_stack

clean: ## remove caches and build output
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info

# ── Workflow helpers (tmp/ is gitignored — local scratch only) ────────────────
# Naming is phase-prefixed: a phase is the ticket (see docs/WORKFLOW.md).

plan: ## new plan file in tmp/plans (PHASE=P01 TITLE=name)
	@mkdir -p tmp/plans
	@printf '# %s — %s\n\n**Goal.** \n\n**Deliverables**\n- \n\n**Acceptance**\n- \n' \
		"$(PHASE)" "$(TITLE)" > tmp/plans/$(PHASE)-$(TITLE).md
	@echo "wrote tmp/plans/$(PHASE)-$(TITLE).md"

handoff: ## new handoff note in tmp/handoff (PHASE=P01 TITLE=name)
	@mkdir -p tmp/handoff
	@printf '# Handoff — %s %s\n\n_Written %s._\n\n## What was completed\n- \n\n## Key findings\n- \n\n## Next phase\n- \n\n## Must not get wrong\n- \n\n## Repo state right now\n- \n' \
		"$(PHASE)" "$(TITLE)" "$$(date +%Y-%m-%d)" > tmp/handoff/$(PHASE)-$(TITLE).md
	@echo "wrote tmp/handoff/$(PHASE)-$(TITLE).md"

smoke: ## new smoke-test stub in tmp/smoke_tests (PHASE=P01 TITLE=name)
	@mkdir -p tmp/smoke_tests
	@printf '#!/usr/bin/env python3\n"""%s %s smoke test — real dependencies, no mocks on the changed path.\n\nUsage:\n    uv run python tmp/smoke_tests/%s-%s-smoke.py\n"""\n\n\ndef main() -> None:\n    raise SystemExit("TODO: implement smoke test")\n\n\nif __name__ == "__main__":\n    main()\n' \
		"$(PHASE)" "$(TITLE)" "$(PHASE)" "$(TITLE)" > tmp/smoke_tests/$(PHASE)-$(TITLE)-smoke.py
	@echo "wrote tmp/smoke_tests/$(PHASE)-$(TITLE)-smoke.py"

tree: ## list the project's files
	@find . -path ./.venv -prune -o -path ./.git -prune -o -path ./tmp -prune -o -type f -print \
		| grep -v -E '\.(pyc|lock)$$' | sort
