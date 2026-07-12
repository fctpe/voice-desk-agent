.PHONY: help install console dev test eval eval-list lint format check

help:
	@echo "voice-desk-agent — make targets"
	@echo "  make install    uv sync (deps + dev tools)"
	@echo "  make console    run the local voice demo (needs OPENAI_API_KEY)"
	@echo "  make dev        run the worker in dev mode (needs LIVEKIT_* + keys)"
	@echo "  make test       run the offline unit suite"
	@echo "  make eval       run all evals (needs OPENAI_API_KEY)"
	@echo "  make eval-list  list eval scenarios (no LLM calls)"
	@echo "  make lint       ruff check"
	@echo "  make format     ruff format"
	@echo "  make check      lint + format --check + tests"

install:
	uv sync

console:
	uv run python -m voice_desk console

dev:
	uv run python -m voice_desk dev

test:
	uv run pytest

eval:
	uv run python evals/run_evals.py

eval-list:
	uv run python evals/run_evals.py --list

lint:
	uv run ruff check .

format:
	uv run ruff format .

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest
