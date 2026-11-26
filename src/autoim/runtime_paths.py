"""Canonical paths for the source checkout, its data, and isolated test runs."""

from dataclasses import dataclass
from pathlib import Path
import os
import tempfile
import tomllib


def project_root(source=None):
    source = Path(source or __file__).resolve()
    for root in source.parents:
        if (root / "src" / "autoim" / "runtime_paths.py").is_file() and (root / "AGENTS.md").is_file():
            try:
                data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("project", {}).get("name") == "autoim":
                return root
    raise RuntimeError("AUTOIM_PROJECT_ROOT_NOT_FOUND: start from an AutoIM source checkout")


@dataclass(frozen=True)
class RuntimePaths:
    project: Path
    root: Path

    @property
    def config(self):
        return self.root / "config"

    @property
    def account(self):
        return self.config / "core-default-account.json"

    @property
    def account_backup(self):
        return self.config / "core-default-account.backup.json"

    @property
    def data(self):
        return self.root / "data"

    @property
    def formal_db(self):
        return self.data / "wecom-visual.sqlite3"

    @property
    def isolated_db(self):
        return self.data / "wecom-test.sqlite3"

    @property
    def pending(self):
        return self.root / "pending"

    @property
    def pending_db(self):
        return self.pending / "observations.sqlite3"

    @property
    def logs(self):
        return self.root / "logs"

    @property
    def diagnostics(self):
        return self.root / "diagnostics"

    @property
    def evidence(self):
        return self.diagnostics / "visual-evidence"

    @property
    def backups(self):
        return self.root / "backups"

    @property
    def databases(self):
        return {"formal": self.formal_db, "isolated": self.isolated_db, "pending": self.pending_db}

    def initialize(self):
        existed = self.root.exists()
        for directory in (self.config, self.data, self.pending, self.logs, self.diagnostics, self.backups):
            if directory.resolve() != directory:
                raise RuntimeError("AUTOIM_RUNTIME_PATH_REDIRECTED")
            directory.mkdir(parents=True, exist_ok=True)
        return "EXISTING" if existed else "INITIALIZED"

    def status(self):
        return {"project": str(self.project), "runtime": str(self.root), "config": str(self.account),
                "data": {name: str(path) for name, path in self.databases.items()},
                "logs": str(self.logs), "diagnostics": str(self.diagnostics), "backups": str(self.backups)}


def runtime_paths():
    project = project_root()
    override = os.environ.get("AUTOIM_TEST_RUNTIME_ROOT")
    real = project / "runtime"
    if not override:
        return RuntimePaths(project, real)
    root = Path(override).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if (root.parent != temporary or not root.name.startswith("autoim-regression-")
            or not (root / ".autoim-test-root").is_file()
            or real.resolve().is_relative_to(root) or root.is_relative_to(real.resolve())):
        raise RuntimeError("AUTOIM_TEST_RUNTIME_ROOT_INVALID")
    return RuntimePaths(project, root)


def legacy_app_data():
    """Used only by the explicit v3.5.3.4 migration."""
    base = os.environ.get("LOCALAPPDATA")
    return Path(base).resolve() / "AutoIM" if base else None
