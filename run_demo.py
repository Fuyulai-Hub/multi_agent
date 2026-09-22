"""一键演示脚本：python run_demo.py [--profile lean|strict]"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mas.cli import main  # noqa: E402

if __name__ == "__main__":
    args = ["demo"] + sys.argv[1:]
    raise SystemExit(main(args))
