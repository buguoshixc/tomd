#!/usr/bin/env python
"""Run tomd straight from a checkout, without installing anything.

    python scripts/convert.py report.pdf
    python scripts/convert.py ./docs --out ./out

Equivalent to ``python -m tomd`` once the package is on the path (which this
script arranges by prepending the repository root).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tomd.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
