"""
codebud/forensic.py

Argument rules for imaging and metadata tools that a word list cannot
express (phase 10, step 5 plan, PR 3b). They apply wherever a profile
allow-lists the tool -- security-ir does -- and keep it to working on
evidence the way NIST SP 800-86 (section 4.2.2) and ACPO principle 3
ask: on copies, hashed, logged, never writing to a device or outside the
project.

- dc3dd: input an image file inside the project, never a device; every
  output and log inside the project; hash= and a log required; wipe and
  pattern writes refused; unknown keys refused.
- ewfacquire: unattended (-u: in a step nobody can answer its prompts)
  and so the case metadata given on the command line (-C -e -E -D),
  -d with sha256, a log (-l), the target (-t) and any second target
  inside the project, the source an image file inside the project;
  options not known to be safe refused.
- exiftool: only reading. Its format strings (${...}), -if conditions and
  -config files run Perl code, and tag values are written with =, < and
  >: those, and the options that write, run or read arguments from a
  file, are refused. Read options and tag names (-Artist) are fine.

Rules were written from the tools' manual pages (dc3dd(1),
ewfacquire(1), exiftool(1)).
"""

import os
import re

# --- paths -------------------------------------------------------------------


def _inside_project(path: str) -> bool:
    """Whether ``path`` (symlinks followed) is inside the current project
    directory and is not a device."""
    real = os.path.realpath(path)
    if real.startswith("/dev/") or real == "/dev":
        return False
    project = os.path.realpath(os.getcwd())
    return os.path.commonpath([real, project]) == project


def _new_output(path: str) -> str | None:
    """Why ``path`` may not be written, or None: it must be inside the
    project, not a device, and not exist yet -- so an output can never
    overwrite the input or another piece of evidence."""
    if not _inside_project(path):
        return "must be inside the project, not a device"
    if os.path.lexists(path):
        return "exists already: outputs and logs must be new files"
    return None


def _image_file(path: str) -> bool:
    """An existing regular file inside the project (an image to work on)."""
    return _inside_project(path) and os.path.isfile(path)


# --- dc3dd -------------------------------------------------------------------

_DC3DD_INPUT = {"if", "ifs"}
_DC3DD_OUTPUT = {"of", "hof", "ofs", "hofs", "log", "hlog", "mlog"}
_DC3DD_REFUSED = {
    "wipe": "wipes",
    "hwipe": "wipes",
    "pat": "writes a pattern",
    "tpat": "writes a pattern",
}
_DC3DD_VALUES = {
    "hash": re.compile(r"^(md5|sha1|sha256|sha512)(,(md5|sha1|sha256|sha512))*$"),
    "cnt": re.compile(r"^\d+$"),
    "iskip": re.compile(r"^\d+$"),
    "oskip": re.compile(r"^\d+$"),
    "bufsz": re.compile(r"^\d+[KMG]?$"),
    "ssz": re.compile(r"^\d+$"),
    "verb": re.compile(r"^on$"),
    "nwspc": re.compile(r"^on$"),
    "rec": re.compile(r"^off$"),
}


def _dc3dd(args: list[str]) -> str | None:
    keys = {}
    for arg in args:
        key, sep, value = arg.partition("=")
        if not sep:
            return f"{arg}: dc3dd takes key=value arguments only"
        if key in _DC3DD_REFUSED:
            return f"{key}= {_DC3DD_REFUSED[key]}"
        if key in _DC3DD_INPUT:
            if not _image_file(value):
                return (
                    f"{key}={value}: input must be an image file inside the project, not a device"
                )
        elif key in _DC3DD_OUTPUT:
            problem = _new_output(value)
            if problem:
                return f"{key}={value}: {problem}"
        elif key in _DC3DD_VALUES:
            if not _DC3DD_VALUES[key].match(value):
                return f"{key}={value}: not an accepted value"
        else:
            return f"{key}=: not a known safe dc3dd option"
        keys[key] = value
    if not keys.keys() & _DC3DD_INPUT:
        return "dc3dd needs an input image (if=)"
    if "hash" not in keys:
        return "dc3dd must hash what it copies (hash=sha256)"
    if not keys.keys() & {"log", "hlog"}:
        return "dc3dd must log what it does (log= or hlog=)"
    return None


# --- ewfacquire --------------------------------------------------------------

# Options that take a value; paths among them must stay inside the project.
_EWF_VALUE = {"-A", "-b", "-B", "-c", "-C", "-d", "-D", "-e", "-E", "-f", "-l", "-m",
              "-M", "-N", "-o", "-p", "-P", "-r", "-S", "-t", "-T", "-2"}  # fmt: skip
_EWF_PATHS = {"-l", "-t", "-2"}
_EWF_SEGMENTS = (".E01", ".Ex01", ".e01", ".s01", ".L01", ".Lx01")
_EWF_FLAGS = {"-u", "-q", "-v", "-R", "-s", "-x", "-h", "-V"}
_EWF_REQUIRED = {
    "-u": "unattended (-u): in a step nobody can answer its prompts",
    "-C": "the case number (-C)",
    "-e": "the examiner (-e)",
    "-E": "the evidence number (-E)",
    "-D": "a description (-D)",
    "-l": "a log file (-l)",
    "-t": "the target (-t)",
}


def _ewfacquire(args: list[str]) -> str | None:
    given, sources = {}, []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _EWF_VALUE:
            if i + 1 >= len(args):
                return f"{arg} needs a value"
            value = args[i + 1]
            if arg in _EWF_PATHS:
                problem = _new_output(value)
                if problem:
                    return f"{arg} {value}: {problem}"
                if arg != "-l":
                    # A target is a base name; its segment files must be new too.
                    existing = [value + e for e in _EWF_SEGMENTS if os.path.lexists(value + e)]
                    if existing:
                        return f"{arg} {value}: {existing[0]} exists already: images are never overwritten"
            given[arg] = value
            i += 2
            continue
        if arg in _EWF_FLAGS:
            given[arg] = ""
        elif arg.startswith("-"):
            return f"{arg}: not a known safe ewfacquire option"
        else:
            sources.append(arg)
        i += 1
    for option, what in _EWF_REQUIRED.items():
        if option not in given:
            return f"ewfacquire needs {what}"
    if "sha256" not in given.get("-d", "").split(","):
        return "ewfacquire must compute a SHA-256 (-d sha256)"
    if len(sources) != 1 or not _image_file(sources[0]):
        return "ewfacquire's source must be one image file inside the project, not a device"
    return None


# --- exiftool ----------------------------------------------------------------

# Options that write, run code, or take arguments from somewhere unseen
# (compared lower-case, before any "=").
_EXIF_REFUSED = {
    "-o", "-out", "-overwrite_original", "-overwrite_original_in_place",
    "-tagsfromfile", "-geotag", "-geosync", "-geotime", "-config", "-p",
    "-printformat", "-api", "-@", "-execute", "-srcfile", "-delete_original",
    "-delete_original!", "-restore_original", "-textout", "-tagout", "-k",
    "-pause", "-stay_open", "-common_args", "-userparam", "-wm", "-writemode",
}  # fmt: skip
_EXIF_REFUSED_FORMS = re.compile(r"^(-if\d*|-w[!+]*|-execute\d*)$")


def _exiftool(args: list[str]) -> str | None:
    for arg in args:
        if "${" in arg:
            return f"{arg}: exiftool runs Perl code in ${{...}}"
        if not arg.startswith("-"):
            continue  # a file or a value
        if any(c in arg for c in "=<>"):
            return f"{arg}: writes, copies or renames tags"
        name = arg.lower()
        if name in _EXIF_REFUSED or _EXIF_REFUSED_FORMS.match(name):
            return f"{arg}: writes, runs code or reads arguments from a file"
    return None


_RULES = {"dc3dd": _dc3dd, "ewfacquire": _ewfacquire, "exiftool": _exiftool}
PROGRAMS = frozenset(_RULES)


def argument_refusal(program: str, args: list[str]) -> str | None:
    """Why ``program args`` is refused by these rules, or None (also for
    programs without rules)."""
    rule = _RULES.get(program)
    return rule(args) if rule else None
