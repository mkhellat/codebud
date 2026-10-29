# docs/Makefile.am — the GNU Texinfo manual.
#
# automake builds codebud.info (installed and registered with install-info),
# and `make html` / `make pdf` build the other formats. version.texi is
# generated from the package version; tests/test_build.py fails if a
# chapter is missing here.

info_TEXINFOS = codebud.texi
codebud_TEXINFOS = \
	chapters/architecture.texi \
	chapters/configuration.texi \
	chapters/development.texi \
	chapters/glossary.texi \
	chapters/installation.texi \
	chapters/introduction.texi \
	chapters/llm-execution.texi \
	chapters/model-management.texi \
	chapters/performance-benchmarking.texi \
	chapters/privacy.texi \
	chapters/safety-and-sandbox.texi \
	chapters/tools-reference.texi \
	chapters/troubleshooting.texi \
	chapters/usage.texi

AM_MAKEINFOHTMLFLAGS = --split=node

MAINTAINERCLEANFILES = Makefile.in
