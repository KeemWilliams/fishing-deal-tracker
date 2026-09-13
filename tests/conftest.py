"""Ensure the `fpt` package is importable regardless of how pytest is invoked
(e.g. `pytest` from this directory vs. `python -m pytest` from the repo
root). Adds the package root (the parent of this `tests/` directory) to
`sys.path` once, at collection time.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))
