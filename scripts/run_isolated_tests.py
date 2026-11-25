"""Run the Python regression suite with an isolated Windows application data root."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    temporary_root = Path(tempfile.gettempdir()).resolve()
    real_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local").resolve()
    if temporary_root == real_app_data / "AutoIM" or temporary_root.is_relative_to(real_app_data / "AutoIM"):
        raise RuntimeError("The system temporary directory is inside the live AutoIM data root")
    with tempfile.TemporaryDirectory(prefix="autoim-regression-", dir=temporary_root) as folder:
        isolated = Path(folder).resolve()
        if isolated.parent != temporary_root or isolated == real_app_data:
            raise RuntimeError("The regression data directory could not be isolated")
        env = {**os.environ, "LOCALAPPDATA": str(isolated), "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v", *sys.argv[1:]],
                              cwd=Path(__file__).resolve().parent.parent, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
