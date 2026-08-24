from __future__ import annotations

import os
import sys
from pathlib import Path

from wechat_bridge.cli import main


def run() -> None:
    data_dir = Path(os.environ.get("WEI_DAILY_DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    sys.argv = [
        "wei-daily-backend",
        "run",
        "--adapter",
        "wechatauto_db",
        "--chat",
        "文件传输助手",
        "--dashboard",
        "--dashboard-host",
        "127.0.0.1",
        "--dashboard-port",
        "8765",
        "--db",
        str(data_dir / "wechat_bridge.db"),
    ]
    main()


if __name__ == "__main__":
    run()
