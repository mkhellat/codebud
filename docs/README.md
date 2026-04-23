# Codebud Documentation

The Codebud manual is written in GNU Texinfo.  It produces HTML, PDF, and
`info` pages from a single source.

## Prerequisites

```bash
sudo pacman -S texinfo          # Arch Linux
# or: sudo apt install texinfo  # Debian/Ubuntu
```

## Build

```bash
cd docs
make html    # → build/html/codebud.html
make pdf     # → build/pdf/codebud.pdf
make info    # → build/info/codebud.info
make all     # all three
make clean   # remove build/
```

## Read online (info)

```bash
info -f docs/build/info/codebud.info
```

## Source layout

```
docs/
├── codebud.texi          top-level manual
├── version.texi          version / date stamps
├── Makefile              build targets
└── chapters/
    ├── introduction.texi
    ├── installation.texi
    ├── model-management.texi
    ├── usage.texi
    ├── configuration.texi
    ├── architecture.texi
    ├── tools-reference.texi
    ├── safety-and-sandbox.texi
    ├── development.texi
    ├── troubleshooting.texi
    └── glossary.texi
```
