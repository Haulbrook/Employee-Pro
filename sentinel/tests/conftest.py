"""Common fixtures + sys.path wiring used by every test module.

Imported automatically by Python's unittest discovery via the package
``__init__.py``. The path injection here mirrors what task_queue.py does at
runtime, so sibling-import patterns (``import ai_engine``) work in tests.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Order: utils first, then each layer dir so its siblings can import each other.
for p in [
    ROOT,
    ROOT / "layer2-agent",
    ROOT / "layer3-autonomy",
    ROOT / "layer4-selfheal",
    ROOT / "layer5-dashboard",
]:
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def make_sentinel_home() -> Path:
    """Create an isolated SENTINEL_HOME for a single test."""
    home = Path(tempfile.mkdtemp(prefix="sentinel-test-"))
    (home / "data").mkdir()
    (home / "data" / "outputs").mkdir()
    (home / "data" / "quarantine").mkdir()
    (home / "config").mkdir()
    os.environ["SENTINEL_HOME"] = str(home)
    os.environ["SENTINEL_LOG_DIR"] = str(home / "log")
    (home / "log").mkdir()
    # invalidate any cached path constants in utils.common
    import importlib
    import utils.common as common
    importlib.reload(common)
    return home
