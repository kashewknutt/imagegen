from __future__ import annotations

import subprocess
import sys


def main() -> None:
    raise SystemExit(
        subprocess.call([sys.executable, "-m", "streamlit", "run", "app.py"])
    )


if __name__ == "__main__":
    main()
