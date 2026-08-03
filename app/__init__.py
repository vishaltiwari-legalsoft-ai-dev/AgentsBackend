"""AgentOS backend package — registers department agents on sys.path."""

from __future__ import annotations

import sys
from pathlib import Path

# Marketing department agents live under "agents/<Agent Name>/<package>". The
# folder names contain spaces, so we put each agent's root on sys.path and
# import the underscore-named package inside it.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_AGENT_ROOTS = [
    _BACKEND_ROOT / "agents" / "Graphics designer agent",
    _BACKEND_ROOT / "agents" / "Marketing Research agent",
    _BACKEND_ROOT / "agents" / "SEO GEO agent",
]
for _root in _AGENT_ROOTS:
    if _root.is_dir() and str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
