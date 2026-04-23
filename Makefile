.PHONY: install install-dev install-skill test lint

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
SKILL_DIR := $(HOME)/.openclaw/skills/codebud

install:
	$(PIP) install -e .

install-dev:
	$(PIP) install -e ".[dev]"

install-skill: install
	mkdir -p $(SKILL_DIR)
	cp openclaw/SKILL.md $(SKILL_DIR)/SKILL.md
	ln -sf $(shell pwd)/.venv/bin/codebud $(HOME)/.local/bin/codebud
	@echo "Skill installed to $(SKILL_DIR)"
	@echo "Run 'openclaw skills list' to verify."

test:
	$(PYTEST) -q

lint:
	$(VENV)/bin/ruff check .
