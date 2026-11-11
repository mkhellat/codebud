# src/Makefile.am — installs the codebud package.
#
# Files are listed explicitly (automake has no wildcards);
# tests/test_build.py fails if a module or data file is missing here.

nobase_python_PYTHON = \
	codebud/__init__.py \
	codebud/__main__.py \
	codebud/cli/__init__.py \
	codebud/cli/admin.py \
	codebud/cli/chat.py \
	codebud/cli/chat_input.py \
	codebud/cli/common.py \
	codebud/cli/display.py \
	codebud/cli/doctor.py \
	codebud/cli/errors.py \
	codebud/cli/index_hints.py \
	codebud/cli/integrity.py \
	codebud/cli/key.py \
	codebud/cli/main.py \
	codebud/cli/parser.py \
	codebud/cli/run.py \
	codebud/cli/steps.py \
	codebud/confine.py \
	codebud/core.py \
	codebud/crypto.py \
	codebud/encrypt_existing.py \
	codebud/executor.py \
	codebud/fsutil.py \
	codebud/gitsafe.py \
	codebud/integrity.py \
	codebud/isolation.py \
	codebud/llm_stub.py \
	codebud/memory.py \
	codebud/modes.py \
	codebud/paths.py \
	codebud/policy.py \
	codebud/planner.py \
	codebud/privacy.py \
	codebud/profiles.py \
	codebud/rag/__init__.py \
	codebud/rag/chunker.py \
	codebud/rag/context.py \
	codebud/rag/files.py \
	codebud/rag/indexer.py \
	codebud/rag/search.py \
	codebud/rag/store.py \
	codebud/redact.py \
	codebud/safety.py \
	codebud/sandbox.py \
	codebud/sessions.py \
	codebud/settings.py \
	codebud/skill.py \
	codebud/tools/__init__.py \
	codebud/tools/commands.py \
	codebud/tools/embeddings.py \
	codebud/tools/file_io.py \
	codebud/tools/patcher.py \
	codebud/tools/tool_registry.py \
	codebud/tools/web_search.py \
	codebud_sec/__init__.py \
	codebud_sec/engagement.py \
	codebud_sec/evidence.py

nobase_dist_python_DATA = \
	codebud/data/policy/harmless_commands.json \
	codebud/data/policy/powerful_commands.json \
	codebud/data/policy/trusted_sequences.json \
	codebud/data/skill/SKILL.md \
	codebud/data/skill/agensi/SKILL.md

# A minimal dist-info directory, so importlib.metadata (and therefore
# `codebud version`) knows the installed version without pip.
distinfodir = $(pythondir)/codebud-$(VERSION).dist-info

install-data-hook:
	$(MKDIR_P) "$(DESTDIR)$(distinfodir)"
	printf 'Metadata-Version: 2.1\nName: codebud\nVersion: %s\n' '$(VERSION)' \
	  > "$(DESTDIR)$(distinfodir)/METADATA"
	printf 'autotools\n' > "$(DESTDIR)$(distinfodir)/INSTALLER"

uninstall-hook:
	rm -f "$(DESTDIR)$(distinfodir)/METADATA" "$(DESTDIR)$(distinfodir)/INSTALLER"
	-rmdir "$(DESTDIR)$(distinfodir)" 2>/dev/null

MAINTAINERCLEANFILES = Makefile.in
