"""Top-of-tree pytest configuration for the referee-server test suite.

This conftest is discovered automatically when pytest's rootdir is the
repository root (see ``pyproject.toml``'s ``testpaths``). It performs the two
import-time chores that every test file previously had to repeat:

1. Put ``referee-server/`` on ``sys.path`` so modules like ``scheduler``,
   ``poller``, and ``db`` import by their bare name without requiring the
   project to be installed as a package.

2. Deterministically decide whether to use the real ``paramiko`` or a minimal
   stub. The previous per-file pattern (``if "paramiko" not in sys.modules``)
   was order-dependent: whichever test file imported first won, producing
   different test behavior on different machines. Here we attempt the real
   import exactly once and only fall back to a stub when the real dependency
   is genuinely unavailable (e.g. a static-analysis runner without SSH libs).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_REFEREE_SERVER_DIR = Path(__file__).resolve().parent
if str(_REFEREE_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_REFEREE_SERVER_DIR))

try:  # pragma: no cover - exercised by environment, not tests
    import paramiko  # noqa: F401
except ImportError:  # pragma: no cover - only hit on stripped-down runners
    sys.modules["paramiko"] = types.SimpleNamespace(
        SSHClient=object,
        RejectPolicy=object,
        AutoAddPolicy=object,
    )
