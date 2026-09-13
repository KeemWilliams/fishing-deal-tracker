#!/usr/bin/env python3
"""Thin entrypoint — see fpt/cli.py for the actual CLI.

    python run.py tick
    python run.py tick --max-discovery-pages 1
"""

import sys

from fpt.cli import main

if __name__ == "__main__":
    sys.exit(main())
