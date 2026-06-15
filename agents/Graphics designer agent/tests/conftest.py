"""Make the agent package importable and isolate run storage for tests."""

import os
import sys
import tempfile
from pathlib import Path

# Agent root contains the `graphics_designer_agent` package.
AGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_ROOT))

# Sandbox runs + force the offline mock provider before anything imports runs.py.
os.environ.setdefault("GD_RUNS_DIR", tempfile.mkdtemp(prefix="gd_runs_"))
os.environ.setdefault("GD_IMAGE_PROVIDER", "mock")
