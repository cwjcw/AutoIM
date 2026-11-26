"""Run all regression tests under a separate, explicitly marked runtime root."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from autoim.runtime_paths import project_root


def main():
    temporary = Path(tempfile.gettempdir()).resolve()
    real = (project_root() / "runtime").resolve()
    if temporary.is_relative_to(real):
        raise RuntimeError("The system temporary directory is inside the live AutoIM runtime")
    with tempfile.TemporaryDirectory(prefix="autoim-regression-", dir=temporary) as folder:
        root = Path(folder).resolve()
        if root.parent != temporary or real.is_relative_to(root) or root.is_relative_to(real):
            raise RuntimeError("The regression data directory could not be isolated")
        (root / ".autoim-test-root").touch()
        env = {key: value for key, value in os.environ.items()
               if key != "GIT_CONFIG_COUNT" and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
        env.update(AUTOIM_TEST_RUNTIME_ROOT=str(root), PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v", *sys.argv[1:]],
                              cwd=project_root(), env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
