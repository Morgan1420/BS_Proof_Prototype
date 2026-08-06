"""Pytest configuration shared across the test suite.

Ensures the ``backend`` directory (the parent of this ``tests`` package)
is importable as ``app.*`` regardless of the directory pytest is invoked
from.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
