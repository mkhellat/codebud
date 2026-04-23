# Makefile — Codebud build system
# Follows GNU Coding Standards (https://www.gnu.org/prep/standards/).

# ─── Installation directories ────────────────────────────────────────────────
# Override PREFIX at the command line:  make install PREFIX=/usr/local
PREFIX  ?= $(HOME)/.local
BINDIR   = $(PREFIX)/bin

# ─── Project variables ───────────────────────────────────────────────────────
PYTHON_INTERP ?= python3
VENV           = .venv
PYTHON         = $(VENV)/bin/python
PIP            = $(VENV)/bin/pip
PYTEST         = $(VENV)/bin/pytest
RUFF           = $(VENV)/bin/ruff
DOCS_DIR       = docs
SKILL_DIR      = $(HOME)/.openclaw/skills/codebud

# Stamp files — let make track whether a target is up to date
STAMP_VENV = $(VENV)/.stamp-venv
STAMP_DEV  = $(VENV)/.stamp-dev

# ─── Default target ──────────────────────────────────────────────────────────
.DEFAULT_GOAL := help

# ─── Phony targets ───────────────────────────────────────────────────────────
.PHONY: all venv install install-dev install-skill \
        uninstall uninstall-skill \
        check test coverage lint format \
        docs html pdf info docs-clean \
        dist clean distclean \
        help


# ─── Help ────────────────────────────────────────────────────────────────────

help:
	@printf '%s\n' \
	  'Codebud build system — GNU-standard targets' \
	  '' \
	  'SETUP' \
	  '  make venv            Create the Python virtual environment (.venv/)' \
	  '  make install         Install codebud in editable mode (dev workflow)' \
	  '  make install-dev     Install with all development dependencies' \
	  '  make install-skill   Register the OpenClaw skill; link binary to BINDIR' \
	  '' \
	  'UNINSTALL' \
	  '  make uninstall       Remove binary link; pip-uninstall the package' \
	  '  make uninstall-skill Remove the OpenClaw skill registration and binary link' \
	  '' \
	  'DEVELOPMENT' \
	  '  make check           Run the test suite (alias for test)' \
	  '  make test            Run the full test suite with pytest' \
	  '  make coverage        Run tests and write an HTML coverage report' \
	  '  make lint            Check code style with ruff' \
	  '  make format          Auto-format and auto-fix code with ruff' \
	  '' \
	  'DOCUMENTATION' \
	  '  make docs            Build all documentation (html + pdf + info)' \
	  '  make html            Build HTML documentation only' \
	  '  make pdf             Build PDF documentation only' \
	  '  make info            Build GNU info documentation only' \
	  '  make docs-clean      Remove documentation build artefacts' \
	  '' \
	  'DISTRIBUTION' \
	  '  make dist            Build source distribution (.tar.gz) and wheel (.whl)' \
	  '' \
	  'CLEANUP' \
	  '  make clean           Remove build artefacts and caches' \
	  '  make distclean       Remove everything not tracked by version control' \
	  '' \
	  'VARIABLES (override on the command line)' \
	  '  PREFIX=$(PREFIX)' \
	  '  BINDIR=$(BINDIR)' \
	  '  PYTHON_INTERP=$(PYTHON_INTERP)' \


# ─── Virtual environment ─────────────────────────────────────────────────────
# The stamp file means "venv exists and pip/setuptools are up to date".
# make only re-runs this recipe when the stamp is missing or older than
# pyproject.toml.

$(STAMP_VENV): pyproject.toml
	$(PYTHON_INTERP) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip setuptools wheel
	@touch $@

venv: $(STAMP_VENV)


# ─── Install ─────────────────────────────────────────────────────────────────

# install: editable install of the production package only.
# Suitable for using codebud as a tool; not for running the test suite.
install: $(STAMP_VENV)
	$(PIP) install -e .

# install-dev: editable install with all development dependencies.
# Uses a stamp file so repeated calls skip the work when nothing changed.
$(STAMP_DEV): $(STAMP_VENV) pyproject.toml
	$(PIP) install --quiet -e ".[dev]"
	@touch $@

install-dev: $(STAMP_DEV)

# install-skill: register codebud with the OpenClaw gateway.
# Copies SKILL.md to the managed skills directory and links the binary.
install-skill: install
	@mkdir -p $(SKILL_DIR)
	cp openclaw/SKILL.md $(SKILL_DIR)/SKILL.md
	ln -sf $(CURDIR)/$(VENV)/bin/codebud $(BINDIR)/codebud
	@printf 'Skill registered at %s\n' '$(SKILL_DIR)'
	@printf 'Binary linked at   %s\n' '$(BINDIR)/codebud'
	@printf "Run 'openclaw skills list' to verify.\n"


# ─── Uninstall ───────────────────────────────────────────────────────────────

uninstall:
	-$(PIP) uninstall -y codebud
	-rm -f $(BINDIR)/codebud
	@printf 'codebud removed from %s\n' '$(BINDIR)'

uninstall-skill:
	-rm -rf $(SKILL_DIR)
	-rm -f $(BINDIR)/codebud
	@printf 'OpenClaw skill and binary link removed.\n'


# ─── Development ─────────────────────────────────────────────────────────────

check: test

test: $(STAMP_DEV)
	$(PYTEST) tests/

# Run pytest with branch coverage; write an HTML report to htmlcov/.
coverage: $(STAMP_DEV)
	$(PYTEST) \
	  --cov=agent --cov=run_agent \
	  --cov-branch \
	  --cov-report=term-missing \
	  --cov-report=html:htmlcov \
	  tests/
	@printf '\nCoverage report written to htmlcov/index.html\n'

lint: $(STAMP_DEV)
	$(RUFF) check .

# format runs ruff format (whitespace/line-length) then ruff check --fix
# (auto-fixable style issues such as import ordering).
format: $(STAMP_DEV)
	$(RUFF) format .
	$(RUFF) check --fix .


# ─── Documentation ───────────────────────────────────────────────────────────
# Delegate to docs/Makefile so the docs/ tree stays self-contained.

docs: html pdf info

html:
	$(MAKE) -C $(DOCS_DIR) html

pdf:
	$(MAKE) -C $(DOCS_DIR) pdf

info:
	$(MAKE) -C $(DOCS_DIR) info

docs-clean:
	$(MAKE) -C $(DOCS_DIR) clean


# ─── Distribution ────────────────────────────────────────────────────────────
# Produces dist/*.tar.gz (sdist) and dist/*.whl (wheel).
# Requires the 'build' package (included in [dev] extras).

dist: $(STAMP_DEV)
	$(PYTHON) -m build


# ─── Cleanup ─────────────────────────────────────────────────────────────────

# clean: remove compiled and test artefacts; keep the venv and dist files.
clean:
	find . \( -name '__pycache__' -o -name '*.pyc' -o -name '*.pyo' \) \
	     -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage
	rm -rf *.egg-info codebud.egg-info
	rm -rf build/ dist/

# distclean: remove everything not tracked by version control.
# After this target the source tree matches a fresh git clone.
distclean: clean docs-clean
	rm -rf $(VENV)
	@printf 'Source tree restored to a pristine state.\n'
