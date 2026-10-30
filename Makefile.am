# Makefile.am — GNU Automake input for codebud (top level).
#
#   ./configure && make && make check && make install
#
# Standard GNU targets (all, check, install, uninstall, installcheck, dist,
# distcheck, clean, distclean, info, html, pdf) come from automake.
# Developer targets are below; `make help` lists them.

SUBDIRS = src docs

# ─── The codebud command ─────────────────────────────────────────────────────
# A small POSIX sh launcher for the configured Python and package directory.

bin_SCRIPTS = codebud
CLEANFILES = codebud

codebud: $(srcdir)/src/launcher.in Makefile
	$(AM_V_GEN)rm -f $@ $@.tmp && \
	$(SED) -e 's|@PYTHON[@]|$(PYTHON)|g' \
	       -e 's|@pythondir[@]|$(pythondir)|g' \
	       $(srcdir)/src/launcher.in > $@.tmp && \
	chmod +x $@.tmp && mv $@.tmp $@

# ─── Tests ───────────────────────────────────────────────────────────────────
# The suite runs against the source tree (src/ on PYTHONPATH), so it also
# works in a read-only source directory, as in `make distcheck`.

check-local:
if HAVE_PYTEST
	cd "$(abs_srcdir)" && \
	PYTHONPATH="$(abs_srcdir)/src$${PYTHONPATH:+:$$PYTHONPATH}" \
	PYTHONDONTWRITEBYTECODE=1 \
	"$(PYTHON)" -m pytest -p no:cacheprovider tests
else
	@echo "pytest is not available for $(PYTHON); skipping the test suite"
endif

# After `make install`: the installed command runs and reports this version.
installcheck-local:
	"$(bindir)/codebud" version | grep "^codebud  $(VERSION)$$"

# ─── Release tarball ─────────────────────────────────────────────────────────

EXTRA_DIST = bootstrap pyproject.toml README.md NEWS llms.txt src/launcher.in tests scripts

# distcheck reconfigures from scratch; keep the Python it was run with.
AM_DISTCHECK_CONFIGURE_FLAGS = PYTHON="$(PYTHON)"

dist-hook:
	find "$(distdir)" -name __pycache__ -type d -prune -exec rm -rf {} +

# ─── Developer targets ───────────────────────────────────────────────────────

VENV = .venv
VENVPY = $(VENV)/bin/python
LINKDIR = $(HOME)/.local/bin

help:
	@printf '%s\n' \
	  'Build and install (GNU standard targets):' \
	  '  make                  build' \
	  '  make check            run the test suite (needs pytest)' \
	  '  make install          install codebud (prefix: $(prefix))' \
	  '  make installcheck     check the installed codebud' \
	  '  make uninstall        remove what make install installed' \
	  '  make info|html|pdf    build the manual' \
	  '  make dist|distcheck   build / verify the release tarball' \
	  '' \
	  'Development (in a git checkout):' \
	  '  make install-dev      editable install with the dev tools in .venv/' \
	  '  make lint             ruff check' \
	  '  make format           ruff format + ruff check --fix' \
	  '  make coverage         tests with branch coverage -> htmlcov/' \
	  '  make dist-python      Python sdist and wheel -> dist/' \
	  '  make install-link     link .venv/bin/codebud into LINKDIR ($(LINKDIR))' \
	  '  make uninstall-link   remove that link' \
	  '  make clean-dev        remove bytecode, tool caches, egg-info (keeps .venv/)' \
	  '  make maintainer-clean back to a fresh checkout (then run ./bootstrap)' \
	  '  make clean-venv       remove .venv/'

install-dev:
	test -x "$(VENVPY)" || "$(PYTHON)" -m venv "$(VENV)"
	"$(VENVPY)" -m pip install --quiet --upgrade pip setuptools
	"$(VENVPY)" -m pip install --quiet -e "$(srcdir)" \
	  --group "$(srcdir)/pyproject.toml:dev"

lint:
	"$(VENVPY)" -m ruff check "$(srcdir)"

format:
	"$(VENVPY)" -m ruff format "$(srcdir)"
	"$(VENVPY)" -m ruff check --fix "$(srcdir)"

coverage:
	cd "$(abs_srcdir)" && "$(abs_builddir)/$(VENVPY)" -m pytest \
	  --cov=codebud --cov-branch --cov-report=term-missing \
	  --cov-report=html:htmlcov tests
	@echo "Coverage report written to htmlcov/index.html"

dist-python:
	"$(VENVPY)" -m build --outdir dist "$(srcdir)"

install-link:
	$(MKDIR_P) "$(LINKDIR)"
	ln -sf "$(abs_builddir)/$(VENV)/bin/codebud" "$(LINKDIR)/codebud"
	@echo "Linked $(LINKDIR)/codebud"

uninstall-link:
	rm -f "$(LINKDIR)/codebud"

clean-local:
	rm -rf htmlcov .coverage build dist

# ─── Back to a fresh checkout ───────────────────────────────────────────────
# `make maintainer-clean` (GNU standard: remove everything that can be
# rebuilt) also removes what ./bootstrap generated, the release tarball and
# the developer caches. Afterwards run ./bootstrap again. .venv/ is kept;
# `make clean-venv` removes it.

MAINTAINERCLEANFILES = Makefile.in aclocal.m4 configure configure~ $(DIST_ARCHIVES)

maintainer-clean-local: clean-dev
	-rm -rf build-aux autom4te.cache

clean-venv:
	rm -rf $(VENV)

# clean-dev: tidy what developer tools leave in a checkout (bytecode, tool
# caches, setuptools build metadata). Kept out of `make clean`, which must
# only remove what make built and must work with a read-only source tree.
# .venv/ is kept.
clean-dev:
	rm -rf .pytest_cache .ruff_cache src/*.egg-info
	find . -path ./$(VENV) -prune -o -name __pycache__ -type d -prune -exec rm -rf {} +

.PHONY: help install-dev lint format coverage dist-python install-link uninstall-link \
        clean-dev clean-venv
