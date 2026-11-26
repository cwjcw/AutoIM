"""SQLite snapshots, non-destructive legacy migration, and local recovery."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic
from uuid import uuid4
import argparse
import json
import os
import re
import shutil
import sqlite3

from autoim.runtime_paths import legacy_app_data, runtime_paths

TABLES = {"formal": ("accounts", "conversations", "messages"),
          "isolated": ("accounts", "conversations", "messages"), "pending": ("batches",)}
CONFIG_FILES = ("core-default-account.json", "core-default-account.backup.json", "settings.ini",
                "wecom-calibration.json", "wecom-pc-visual-calibration.json",
                "wecom-pc-visual-calibration-v12.json", "wecom-pc-visual-thresholds.json")
DAILY_DAYS = 30


class RuntimeDataError(OSError):
    pass


def _now():
    return datetime.now().astimezone()


def _sha(path, deadline=None):
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if deadline is not None:
                _check_time(deadline)
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path, data):
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2).encode())
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _regular(path):
    return path.is_file() and path.resolve() == path.absolute()


def _read_db(path):
    if not _regular(path):
        raise RuntimeDataError("RUNTIME_DATABASE_MISSING_OR_REDIRECTED")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.35)


def _check_time(deadline):
    if monotonic() >= deadline:
        raise RuntimeDataError("RUNTIME_BACKUP_TIME_LIMIT")


def _inspect(db, role, deadline):
    db.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeDataError("RUNTIME_DATABASE_INTEGRITY_FAILED")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = TABLES[role]
    if (role != "pending" and version > 0 or names.intersection(required)) and not set(required) <= names:
        raise RuntimeDataError("RUNTIME_DATABASE_SCHEMA_INVALID")
    # A prior rolled-back readiness probe can leave an empty SQLite file.
    counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
              for table in required if table in names}
    return {"version": version, "counts": counts, "integrity": "OK"}


def _snapshot(src, dst, role, deadline):
    _check_time(deadline)
    with closing(_read_db(src)) as source, closing(sqlite3.connect(dst, timeout=.35)) as dest:
        source.backup(dest, pages=128, progress=lambda *_: _check_time(deadline), sleep=.035)
        dest.execute("PRAGMA journal_mode=DELETE")
        result = _inspect(dest, role, deadline)
    with dst.open("r+b") as stream:
        os.fsync(stream.fileno())
    return {**result, "size": dst.stat().st_size, "sha256": _sha(dst, deadline)}


def _install_new(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.parent.resolve() != dst.parent.absolute():
        raise RuntimeDataError("RUNTIME_PATH_REDIRECTED")
    # The temporary file and destination are on the same volume. A concurrent
    # writer or existing target wins without getting replaced.
    os.link(src, dst)


def _copy_new(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".migrate-", dir=dst.parent) as folder:
        temporary = Path(folder) / "payload"
        with src.open("rb") as source, temporary.open("xb") as out:
            shutil.copyfileobj(source, out)
            out.flush()
            os.fsync(out.fileno())
        if _sha(src) != _sha(temporary):
            raise RuntimeDataError("RUNTIME_MIGRATION_SOURCE_CHANGED")
        _install_new(temporary, dst)


def _existing_assets(source, target):
    if not source.is_dir() or source.is_symlink() or source.is_junction():
        return
    for directory, dirs, files in os.walk(source, followlinks=False):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if not (parent / name).is_symlink() and not (parent / name).is_junction()]
        for name in files:
            path = parent / name
            if path.is_file() and not path.is_symlink():
                yield path, target / path.relative_to(source)


def _legacy_file(source, path):
    if _regular(path):
        return path
    if os.name != "nt" or not path.is_file() or not path.is_relative_to(source):
        return None
    relative = path.relative_to(source)
    for parent in (path, *path.parents):
        if parent == source:
            break
        if parent.is_symlink() or parent.is_junction():
            return None
    # Packaged Windows hosts may present an AppData virtualized view without
    # a filesystem link. Accept only the same relative path in its LocalCache.
    canonical = path.resolve()
    packages = source.parent / "Packages"
    if not canonical.is_relative_to(packages):
        return None
    parts = canonical.relative_to(packages).parts
    if len(parts) == len(relative.parts) + 4 and parts[1:4] == ("LocalCache", "Local", "AutoIM"):
        if parts[4:] == relative.parts and _regular(canonical):
            return canonical
    return None


def migrate_legacy(paths, source=None):
    """Copy current files once. The old data is kept and nothing is merged."""
    paths.initialize()
    source = legacy_app_data() if source is None else Path(source).resolve()
    log = paths.backups / "legacy-migration.json"
    if log.is_file():
        prior = _read_json(log)
        if not any(row["status"] == "ERROR" or (row["status"] == "MISSING" and source
                   and _legacy_file(source, Path(row["source"]))) for row in prior["entries"]):
            return prior
    else:
        prior = {"entries": []}
    if not source or not source.is_dir():
        return {"state": "NO_LEGACY_ROOT", "entries": []}
    if paths.root.resolve().is_relative_to(source) or source.is_relative_to(paths.root.resolve()):
        raise RuntimeDataError("RUNTIME_MIGRATION_PATH_OVERLAP")
    legacy_dbs = {"formal": source / "wecom-visual.sqlite3", "isolated": source / "test-data" / "wecom-test.sqlite3",
                  "pending": source / "pending-collections" / "observations.sqlite3"}
    if any(_regular(path) for path in paths.databases.values()) and not prior["entries"]:
        backup_runtime(paths, "before-migration", budget=30)
    entries = []
    previous = {row["target"]: row for row in prior["entries"] if row["status"] == "COPIED"}

    def migrate(src, dst, role=None):
        if str(dst) in previous and _regular(dst):
            entries.append(previous[str(dst)])
            return
        row = {"source": str(src), "target": str(dst), "type": role or "file"}
        entries.append(row)
        canonical = _legacy_file(source, src)
        if not canonical:
            row["status"] = "MISSING"
        elif dst.exists() or dst.is_symlink():
            identical = not role and _regular(dst) and _sha(canonical) == _sha(dst)
            if not identical and dst.name in CONFIG_FILES[:2] and _regular(dst):
                from autoim.wecom.collection_incremental import CoreAccount
                account = CoreAccount.load(dst)
                identical = account is not None and CoreAccount.load(canonical) == account
            row["status"] = "IDENTICAL" if identical else "CONFLICT"
        else:
            try:
                if role:
                    with TemporaryDirectory(prefix=".migration-", dir=paths.backups) as folder:
                        temporary = Path(folder) / "sqlite.snapshot"
                        row.update(_snapshot(canonical, temporary, role, monotonic() + 30))
                        _install_new(temporary, dst)
                    mark_database(paths, role)
                else:
                    _copy_new(canonical, dst)
                row["status"] = "COPIED"
            except (OSError, sqlite3.Error, RuntimeDataError) as error:
                row.update(status="ERROR", error=getattr(error, "sqlite_errorname", None) or type(error).__name__)

    for role, destination in paths.databases.items():
        migrate(legacy_dbs[role], destination, role)
    for filename in CONFIG_FILES:
        old = source / filename
        if not old.exists() and filename not in CONFIG_FILES[:2]:
            old = paths.project / "config" / filename
        migrate(old, paths.config / filename)
    for old, new in ((source / "logs", paths.logs), (source / "logs" / "visual-evidence", paths.evidence),
                     (source / "diagnostics", paths.diagnostics), (source / "private-test-crops", paths.diagnostics / "private-test-crops"),
                     (paths.project / "outputs", paths.diagnostics / "legacy-outputs")):
        for src, dst in _existing_assets(old, new):
            if old == source / "logs" and "visual-evidence" in src.relative_to(old).parts:
                continue
            migrate(src, dst)
    report = {"state": "COMPLETED_WITH_ISSUES" if any(r["status"] in ("ERROR", "CONFLICT") for r in entries)
              else "COMPLETED", "at": _now().isoformat(), "entries": entries}
    _atomic_json(log, report)
    return report


def _manifest(folder):
    if (not re.fullmatch(r"\d{8}-\d{6}-[a-z-]+-[0-9a-f]{8}", folder.name)
            or folder.is_symlink() or folder.is_junction() or not _regular(folder / "manifest.json")):
        return None
    try:
        manifest = _read_json(folder / "manifest.json")
        if (manifest["schema"] == 1 and manifest["id"] == folder.name and isinstance(manifest["files"], dict)
                and isinstance(manifest["reason"], str) and datetime.fromisoformat(manifest["at"]).utcoffset() is not None):
            return manifest
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _archives(paths):
    for folder in paths.backups.iterdir() if paths.backups.exists() else ():
        if folder.is_dir() and (manifest := _manifest(folder)):
            yield folder, manifest


def _verified_file(folder, row, name, deadline=None):
    if row["file"] != name:
        raise RuntimeDataError("RUNTIME_BACKUP_NAME_INVALID")
    path = folder / name
    if not _regular(path) or path.stat().st_size != row["size"] or _sha(path, deadline) != row["sha256"]:
        raise RuntimeDataError("RUNTIME_BACKUP_CHECKSUM_INVALID")
    return path


def _prune_daily(paths, now, deadline):
    cutoff = now - timedelta(days=DAILY_DAYS)
    archives = sorted(((folder, record) for folder, record in _archives(paths) if record["reason"] == "daily"),
                      key=lambda pair: datetime.fromisoformat(pair[1]["at"]), reverse=True)
    kept_roles = set()
    for folder, record in archives:
        try:
            if monotonic() >= deadline:
                return
            files = [_verified_file(folder, row, Path(row["file"]).name, deadline)
                     for row in record["files"].values()]
            if set(folder.iterdir()) != {*files, folder / "manifest.json"}:
                continue
            if set(record["files"]) - kept_roles or datetime.fromisoformat(record["at"]) >= cutoff:
                kept_roles.update(record["files"])
                continue
            for file in files:
                file.unlink()
            (folder / "manifest.json").unlink()
            folder.rmdir()
        except (OSError, KeyError, RuntimeDataError):
            continue


def backup_runtime(paths, reason="daily", *, roles=None, budget=12):
    paths.initialize()
    now = _now()
    deadline = monotonic() + budget
    if reason == "daily":
        for folder, record in _archives(paths):
            if record["reason"] == reason and datetime.fromisoformat(record["at"]).date() == now.date():
                try:
                    for row in record["files"].values():
                        _verified_file(folder, row, Path(row["file"]).name, deadline)
                    if all(not path.exists() or role in record["files"] for role, path in paths.databases.items()):
                        return record
                except (OSError, KeyError, RuntimeDataError):
                    continue
    identifier = f"{now:%Y%m%d-%H%M%S}-{reason}-{uuid4().hex[:8]}"
    destination = paths.backups / identifier
    files = {}
    with TemporaryDirectory(prefix=".backup-", dir=paths.backups) as folder:
        staging = Path(folder) / identifier
        staging.mkdir()
        for role, source in paths.databases.items():
            if (roles is None or role in roles) and source.exists():
                files[role] = {"file": source.name, **_snapshot(source, staging / source.name, role, deadline)}
        for name in CONFIG_FILES:
            source = paths.config / name
            if _regular(source):
                _check_time(deadline)
                _copy_new(source, staging / name)
                snapshot = staging / name
                files[name] = {"file": name, "sha256": _sha(snapshot), "size": snapshot.stat().st_size}
        record = {"schema": 1, "id": identifier, "reason": reason, "at": now.isoformat(), "files": files}
        _atomic_json(staging / "manifest.json", record)
        staging.rename(destination)
    if reason == "daily":
        _prune_daily(paths, now, deadline)
    return record


def config_backup_sources(paths):
    for folder, row in sorted(_archives(paths), key=lambda entry: entry[1]["at"], reverse=True):
        for name in CONFIG_FILES[:2]:
            if name not in row["files"]:
                continue
            try:
                yield _verified_file(folder, row["files"][name], name)
            except (OSError, KeyError, RuntimeDataError):
                continue


def database_role(paths, path):
    return next((name for name, expected in paths.databases.items() if path.absolute() == expected), None)


def mark_database(paths, role):
    marker = paths.backups / f"known-{role}.json"
    paths.backups.mkdir(parents=True, exist_ok=True)
    if not marker.is_file():
        _atomic_json(marker, {"role": role, "first_seen": _now().isoformat()})


def restore_missing(paths, role):
    """Only fill a missing file. An existing unreadable file is preserved."""
    destination = paths.databases[role]
    if destination.exists() or destination.is_symlink():
        raise RuntimeDataError("RUNTIME_RESTORE_TARGET_EXISTS")
    if any(Path(str(destination) + ext).exists() for ext in ("-wal", "-shm", "-journal")):
        raise RuntimeDataError("RUNTIME_RESTORE_ORPHANED_SIDECAR")
    for folder, manifest in sorted(_archives(paths), key=lambda pair: pair[1]["at"], reverse=True):
        if role not in manifest["files"]:
            continue
        try:
            source = _verified_file(folder, manifest["files"][role], destination.name)
            if role == "isolated":
                _verify_isolated_account(paths, source)
            with TemporaryDirectory(prefix=".restore-", dir=paths.backups) as staging:
                temporary = Path(staging) / "sqlite.snapshot"
                stats = _snapshot(source, temporary, role, monotonic() + 6)
                _install_new(temporary, destination)
            mark_database(paths, role)
            report = {"role": role, "backup": manifest["id"], "backup_at": manifest["at"],
                      "restored_at": _now().isoformat(), **stats}
            _atomic_json(paths.backups / f"restore-{role}.json", report)
            return report
        except (OSError, sqlite3.Error, RuntimeDataError):
            if destination.exists():
                raise
    raise RuntimeDataError("RUNTIME_DATABASE_MISSING_NO_VALID_BACKUP")


def _verify_isolated_account(paths, source):
    from autoim.wecom.collection_incremental import CoreAccount
    from autoim.wecom.collection_profile import normalize_email
    primary, backup = CoreAccount.load(paths.account), CoreAccount.load(paths.account_backup)
    if primary and backup and primary != backup:
        raise RuntimeDataError("RUNTIME_RESTORE_ACCOUNT_CONFLICT")
    account = primary or backup
    with closing(_read_db(source)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
            return
        observed = db.execute("""SELECT profile,login_name,email,identity_status FROM accounts
            WHERE profile LIKE 'CORE-LOCAL-%' OR identity_status='LOCAL_DEFAULT_UNVERIFIED'""").fetchall()
    if not observed:
        return
    profile, name, email, state = observed[0]
    email = normalize_email(email) if email else None
    identity = CoreAccount(name, email) if name and email else account
    if (len(observed) != 1 or state != "LOCAL_DEFAULT_UNVERIFIED" or not identity or identity.profile != profile
            or not name or not profile.startswith("CORE-LOCAL-")
            or (account and (account.profile != identity.profile or name.strip() != account.name))):
        raise RuntimeDataError("RUNTIME_RESTORE_ACCOUNT_CONFLICT")


def protect_database(paths, role):
    path = paths.databases[role]
    known = paths.backups / f"known-{role}.json"
    if path.exists() and not _regular(path):
        raise RuntimeDataError("RUNTIME_DATABASE_REDIRECTED")
    if not path.exists() and (known.exists() or any(role in row["files"] for _, row in _archives(paths))):
        restore_missing(paths, role)


def protect_schema_upgrade(paths, role, target_version):
    path = paths.databases[role]
    if not _regular(path) or not path.stat().st_size:
        return
    with closing(_read_db(path)) as db:
        current = db.execute("PRAGMA user_version").fetchone()[0]
        has_data = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
    if has_data and current < target_version:
        backup_runtime(paths, "before-schema", roles={role}, budget=12)


def maintenance_status(paths):
    """A background startup check. A daily backup failure does not grant or revoke navigation."""
    migration = paths.backups / "legacy-migration.json"
    status = {"migration": "NOT_RUN", "backup": "PENDING", "databases": {}}
    if migration.is_file():
        manifest = _read_json(migration)
        status["migration"] = manifest["state"]
        status["migration_issues"] = sum(row["status"] in ("CONFLICT", "ERROR") for row in manifest["entries"])
    for role, target in paths.databases.items():
        was_missing = not target.is_file()
        try:
            protect_database(paths, role)
            if target.is_file():
                with closing(_read_db(target)) as db:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if role != "pending":
                        from autoim.wecom.visual_store import SCHEMA_VERSION
                        if version > SCHEMA_VERSION:
                            raise RuntimeDataError("RUNTIME_DATABASE_VERSION_UNSUPPORTED")
            status["databases"][role] = ("RESTORED" if was_missing and target.is_file()
                                         else "PRESENT" if target.is_file() else "UNINITIALIZED")
            indicator = paths.backups / f"restore-{role}.json"
            if indicator.is_file():
                status.setdefault("restores", {})[role] = _read_json(indicator)
        except (RuntimeDataError, OSError, sqlite3.Error) as error:
            status["databases"][role] = str(error) if isinstance(error, RuntimeDataError) else getattr(error, "sqlite_errorname", None) or type(error).__name__
    try:
        daily = backup_runtime(paths)
        status.update(backup="VERIFIED", backup_id=daily["id"], backup_at=daily["at"], backup_roles=sorted(daily["files"]))
    except (OSError, sqlite3.Error) as error:
        status.update(backup="FAILED", reason=str(error) if isinstance(error, RuntimeDataError) else type(error).__name__)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("migrate", "backup", "restore-missing"))
    parser.add_argument("role", nargs="?", choices=tuple(TABLES))
    args = parser.parse_args()
    paths = runtime_paths()
    paths.initialize()
    if args.action == "migrate":
        report = migrate_legacy(paths)
        summary = {}
        for item in report["entries"]:
            summary[item["status"]] = summary.get(item["status"], 0) + 1
        print(report["state"], summary)
    elif args.action == "backup":
        report = backup_runtime(paths, "manual", budget=60)
        print(report["id"], {role: row.get("counts") for role, row in report["files"].items() if role in TABLES})
    else:
        if not args.role:
            parser.error("restore-missing requires a role")
        print(restore_missing(paths, args.role))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
