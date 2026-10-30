# configure.ac — GNU Autoconf input for codebud.
#
# Generate ./configure with ./bootstrap (autoreconf), then:
#     ./configure && make && make check && make install
#
# Targets GNU/Linux (Debian/Ubuntu, Fedora/RHEL, Arch), macOS and FreeBSD.
# The generated configure is portable POSIX sh.

AC_PREREQ([2.69])

# The version has one source of truth: pyproject.toml.
AC_INIT([codebud],
        m4_esyscmd_s([sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml]),
        [https://github.com/mkhellat/codebud/issues],
        [codebud],
        [https://github.com/mkhellat/codebud])
AC_CONFIG_SRCDIR([src/codebud/__init__.py])
AC_CONFIG_AUX_DIR([build-aux])

# foreign: the GNU file set (NEWS, AUTHORS, ChangeLog) is not required.
AM_INIT_AUTOMAKE([1.16 foreign -Wall -Werror])
AM_SILENT_RULES([yes])

AC_PROG_SED
AC_PROG_MKDIR_P
AC_PROG_LN_S

# ─── Python ───────────────────────────────────────────────────────────────
# Choose a specific interpreter with: ./configure PYTHON=/path/to/python3
AM_PATH_PYTHON([3.10])

# Runtime dependencies come from the system (or the active virtualenv).
# Distribution builds run in clean chroots without them: they can pass
# --disable-dependency-check and declare the dependencies in the package.
AC_ARG_ENABLE([dependency-check],
  [AS_HELP_STRING([--disable-dependency-check],
    [do not require the runtime Python modules at configure time])],
  [], [enable_dependency_check=yes])

AS_IF([test "x$enable_dependency_check" != xno], [
  AC_MSG_CHECKING([for the Python modules codebud needs])
  codebud_missing=
  for codebud_mod in requests prompt_toolkit; do
    "$PYTHON" -c "import $codebud_mod" >/dev/null 2>&1 \
      || codebud_missing="$codebud_missing $codebud_mod"
  done
  # config.toml needs tomllib, in the standard library only from 3.11.
  "$PYTHON" -c "import sys; sys.version_info >= (3, 11) or __import__('tomli')" \
      >/dev/null 2>&1 || codebud_missing="$codebud_missing tomli"
  AS_IF([test -n "$codebud_missing"], [
    AC_MSG_RESULT([missing:$codebud_missing])
    AC_MSG_ERROR([install the missing Python modules, for example:
    Debian/Ubuntu:  apt install python3-requests python3-prompt-toolkit
    Fedora/RHEL:    dnf install python3-requests python3-prompt-toolkit
    Arch Linux:     pacman -S python-requests python-prompt_toolkit
    FreeBSD:        pkg install py311-requests py311-prompt-toolkit
    macOS:          $PYTHON -m pip install requests prompt_toolkit  (in a venv)
(on Python 3.10 also tomli: python3-tomli / python-tomli / py310-tomli)
or point configure at a Python that has them: ./configure PYTHON=/path/to/python3
(--disable-dependency-check skips this check, e.g. for distribution builds)])
  ], [AC_MSG_RESULT([yes])])
  # Older requests have known credential leaks (CVE-2023-32681,
  # CVE-2024-35195, CVE-2024-47081); distributions may backport the fixes,
  # so warn rather than fail.
  AS_IF(["$PYTHON" -c "import requests, sys; v = tuple(int(x) for x in requests.__version__.split('.')[[:3]] if x.isdigit()); sys.exit(v < (2, 32, 4))" >/dev/null 2>&1], [],
    [AC_MSG_WARN([requests is older than 2.32.4, which fixes credential leaks
(CVE-2023-32681, CVE-2024-35195, CVE-2024-47081); upgrade it unless your
distribution has backported those fixes])])
])

# numpy is optional: only project search (codebud index) needs it.
AC_MSG_CHECKING([whether $PYTHON has numpy (for project search)])
AS_IF(["$PYTHON" -c "import numpy" >/dev/null 2>&1],
  [codebud_have_numpy=yes], [codebud_have_numpy=no])
AC_MSG_RESULT([$codebud_have_numpy])
AS_IF([test "x$codebud_have_numpy" = xno], [
  AC_MSG_WARN([numpy not found; project search will be unavailable until it is
installed (apt install python3-numpy, dnf install python3-numpy,
pacman -S python-numpy, pkg install py311-numpy, or pip install numpy)])
])

# pyrage is optional: only encryption at rest (age) needs it.
AC_MSG_CHECKING([whether $PYTHON has pyrage (for encryption at rest)])
AS_IF(["$PYTHON" -c "import pyrage" >/dev/null 2>&1],
  [codebud_have_pyrage=yes], [codebud_have_pyrage=no])
AC_MSG_RESULT([$codebud_have_pyrage])
AS_IF([test "x$codebud_have_pyrage" = xno], [
  AC_MSG_WARN([pyrage not found; encryption at rest will be unavailable until
it is installed (pip install pyrage)])
])

# pytest is only needed for `make check`.
AC_MSG_CHECKING([whether $PYTHON has pytest])
AS_IF(["$PYTHON" -c "import pytest" >/dev/null 2>&1],
  [codebud_have_pytest=yes], [codebud_have_pytest=no])
AC_MSG_RESULT([$codebud_have_pytest])
AM_CONDITIONAL([HAVE_PYTEST], [test "x$codebud_have_pytest" = xyes])

# ─── Ollama (runtime only) ──────────────────────────────────────────────────
AC_PATH_PROG([OLLAMA], [ollama])
AS_IF([test -z "$OLLAMA"], [
  AC_MSG_WARN([ollama was not found; codebud needs a running Ollama server
(https://ollama.com), local or remote via OLLAMA_BASE_URL])
])

# Isolated command steps (runtime only): bubblewrap.
AC_PATH_PROG([BWRAP], [bwrap])
AS_IF([test -z "$BWRAP"], [
  AC_MSG_WARN([bwrap (bubblewrap) was not found; command steps will run without
isolation (no network sandbox, secrets not hidden). Install it with your
package manager: bubblewrap])
])

AC_CONFIG_FILES([Makefile src/Makefile docs/Makefile])
AC_OUTPUT

AC_MSG_NOTICE([
  codebud $PACKAGE_VERSION
    prefix:          $prefix
    Python:          $PYTHON ($PYTHON_VERSION)
    package dir:     $pythondir
    tests (pytest):  $codebud_have_pytest
    project search:  $codebud_have_numpy (numpy)
    encryption:      $codebud_have_pyrage (pyrage)
    ollama:          ${OLLAMA:-not found}
    isolation:       ${BWRAP:-not found (steps not isolated)}
])
