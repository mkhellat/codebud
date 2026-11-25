"""
codebud_sec/__main__.py

Lets `python -m codebud_sec ...` run the codebud-sec command.
"""

import sys

from codebud_sec.cli import main

sys.exit(main())
