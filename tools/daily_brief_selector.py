"""Command-line shim for the tracked daily-brief selector module.

Use ``python tools/daily_brief_selector.py ...`` from a source checkout, or
the ``wechat-daily-brief-select`` console entry point after installation.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wechat_bridge.daily_brief_selector import main  # noqa: E402


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
