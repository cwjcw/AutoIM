from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import json
import os
import sqlite3
import subprocess
import zipfile

import numpy as np

from autoim.runtime_paths import RuntimePaths, project_root, runtime_paths
from autoim.runtime_storage import (RuntimeDataError, backup_runtime, config_backup_sources, maintenance_status,
                                    mark_database, migrate_legacy, protect_database, restore_missing)
from autoim.wecom.collection_incremental import CoreAccount, bind_core_account
from autoim.wecom.collection_pending import PersistentAccount
from autoim.wecom.collection_runtime import CollectionRuntime, repository_state
from autoim.wecom.visual_contacts import recover_b2_start
from autoim.wecom.visual_layout import Box, LayoutObservation, UiRegion, VisualThresholds
from autoim.wecom.visual_messages import Container, analyze_messages
from autoim.wecom.visual_ocr import TextSpan
from autoim.wecom.visual_store import VisualStore

ACCOUNT = CoreAccount("Synthetic account", "core@example.test")


class RuntimeTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="autoim-路径-test-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.paths = RuntimePaths(self.base / "项目 空间", self.base / "项目 空间" / "runtime")
        self.old = self.base / "旧 数据"
        self.old.mkdir()

    def config(self, path, account=ACCOUNT):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": account.name, "email": account.email}), encoding="utf-8")

    def db(self, path, kind="formal"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            if kind == "pending":
                db.execute("CREATE TABLE batches (id TEXT PRIMARY KEY)")
                db.execute("INSERT INTO batches VALUES ('pending')")
            else:
                db.executescript("CREATE TABLE accounts (id INTEGER); CREATE TABLE conversations (id INTEGER);"
                                 "CREATE TABLE messages (id INTEGER); INSERT INTO messages VALUES (1)")
            db.commit()

    def test_root_from_source_with_spaces_and_chinese(self):
        script = self.paths.project / "src" / "autoim" / "runtime_paths.py"
        script.parent.mkdir(parents=True)
        script.touch()
        (self.paths.project / "pyproject.toml").write_text('[project]\nname="autoim"\n', encoding="utf-8")
        (self.paths.project / "AGENTS.md").touch()
        self.assertEqual(project_root(script), self.paths.project)
        self.assertEqual(project_root(), Path(__file__).resolve().parents[1])
        with self.assertRaisesRegex(RuntimeError, "AUTOIM_PROJECT_ROOT_NOT_FOUND"):
            project_root(self.old / "runtime_paths.py")

    def test_default_runtime_from_outside_repo_without_localappdata(self):
        script = "from autoim.runtime_paths import runtime_paths; print(runtime_paths().root)"
        env = {**os.environ, "LOCALAPPDATA": str(self.old), "PYTHONIOENCODING": "utf-8"}
        env.pop("AUTOIM_TEST_RUNTIME_ROOT", None)
        value = subprocess.check_output([os.sys.executable, "-c", script], env=env, cwd=self.old, encoding="utf-8")
        self.assertEqual(Path(value.strip()), project_root() / "runtime")

    def test_test_override_rejects_real_or_unmarked_root(self):
        for path in (project_root(), project_root() / "runtime", self.old):
            with self.subTest(path=path), patch.dict(os.environ, {"AUTOIM_TEST_RUNTIME_ROOT": str(path)}):
                with self.assertRaisesRegex(RuntimeError, "AUTOIM_TEST_RUNTIME_ROOT_INVALID"):
                    runtime_paths()

    def test_initialize_paths_are_independent_and_idempotent(self):
        p = self.paths
        self.assertEqual(p.initialize(), "INITIALIZED")
        self.assertEqual(p.initialize(), "EXISTING")
        self.assertEqual({x.parent for x in p.databases.values()}, {p.data, p.pending})
        self.assertEqual({x.name for x in p.root.iterdir()}, {"config", "data", "pending", "backups", "logs", "diagnostics"})
        self.assertEqual(p.evidence.parent, p.diagnostics)
        self.assertFalse(p.formal_db.exists())

    def test_migration_preserves_role_boundaries_and_reports_missing(self):
        self.db(self.old / "wecom-visual.sqlite3")
        self.db(self.old / "test-data" / "wecom-test.sqlite3")
        self.db(self.old / "pending-collections" / "observations.sqlite3", "pending")
        self.config(self.old / "core-default-account.json")
        original = {p: p.read_bytes() for p in self.old.rglob("*") if p.is_file()}
        report = migrate_legacy(self.paths, self.old)
        self.assertEqual(report["state"], "COMPLETED")
        self.assertTrue(any(row["status"] == "MISSING" for row in report["entries"]))
        self.assertEqual([row["counts"] for row in report["entries"] if row["type"] in self.paths.databases],
                         [{"accounts": 0, "conversations": 0, "messages": 1}] * 2 + [{"batches": 1}])
        self.assertEqual(CoreAccount.load(self.paths.account), ACCOUNT)
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        self.assertEqual(migrate_legacy(self.paths, self.old), report)

    def test_migration_never_overwrites_existing_database(self):
        old = self.old / "wecom-visual.sqlite3"
        self.db(old)
        self.db(self.paths.formal_db)
        with closing(sqlite3.connect(self.paths.formal_db)) as db:
            db.execute("INSERT INTO messages VALUES (2)")
            db.commit()
        result = migrate_legacy(self.paths, self.old)
        self.assertEqual(result["state"], "COMPLETED_WITH_ISSUES")
        self.assertEqual(result["entries"][0]["status"], "CONFLICT")
        with closing(sqlite3.connect(self.paths.formal_db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)

    def test_migration_copies_wal_commit_through_backup_api(self):
        legacy = self.old / "wecom-visual.sqlite3"
        self.db(legacy)
        with closing(sqlite3.connect(legacy)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("INSERT INTO messages VALUES (2)")
            db.commit()
            self.assertTrue(Path(str(legacy) + "-wal").stat().st_size)
            self.assertEqual(migrate_legacy(self.paths, self.old)["entries"][0]["counts"]["messages"], 2)

    def test_migration_keeps_visual_evidence_separate_from_logs(self):
        f = self.old / "logs" / "visual-evidence" / "sample" / "evidence.json"
        f.parent.mkdir(parents=True)
        f.write_text("{}", encoding="utf-8")
        migrate_legacy(self.paths, self.old)
        self.assertTrue((self.paths.evidence / "sample" / "evidence.json").is_file())
        self.assertFalse((self.paths.logs / "visual-evidence").exists())

    def test_missing_known_database_fails_closed_without_backup(self):
        self.paths.initialize()
        mark_database(self.paths, "formal")
        with self.assertRaisesRegex(RuntimeDataError, "MISSING_NO_VALID_BACKUP"):
            protect_database(self.paths, "formal")
        self.assertFalse(self.paths.formal_db.exists())

    def test_daily_backup_restores_missing_database_and_verifies_counts(self):
        self.db(self.paths.formal_db)
        result = backup_runtime(self.paths)
        self.assertEqual(backup_runtime(self.paths), result)
        self.paths.formal_db.unlink()
        report = restore_missing(self.paths, "formal")
        self.assertEqual(report["counts"]["messages"], 1)
        self.assertTrue((self.paths.backups / "restore-formal.json").is_file())
        self.assertTrue(self.paths.formal_db.is_file())
        with self.assertRaisesRegex(RuntimeDataError, "TARGET_EXISTS"):
            restore_missing(self.paths, "formal")

    def test_backup_failure_keeps_previous_archive_and_rejects_corruption(self):
        self.db(self.paths.formal_db)
        result = backup_runtime(self.paths)
        picture = self.paths.backups / result["id"] / self.paths.formal_db.name
        old = picture.read_bytes()
        self.paths.formal_db.write_bytes(b"invalid sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            backup_runtime(self.paths, "manual")
        self.assertEqual(picture.read_bytes(), old)
        self.assertNotEqual(maintenance_status(self.paths)["databases"]["formal"], "PRESENT")
        self.paths.formal_db.unlink()
        picture.write_bytes(b"tampered archive")
        with self.assertRaisesRegex(RuntimeDataError, "MISSING_NO_VALID_BACKUP"):
            restore_missing(self.paths, "formal")

    def test_daily_retention_keeps_protected_and_latest_per_role(self):
        base = datetime(2026, 5, 1).astimezone()
        self.db(self.paths.formal_db)
        self.db(self.paths.pending_db, "pending")
        with patch("autoim.runtime_storage._now", return_value=base):
            first = backup_runtime(self.paths)
            protected = backup_runtime(self.paths, "before-history")
        with patch("autoim.runtime_storage._now", return_value=base + timedelta(days=1)):
            expired = backup_runtime(self.paths)
        with patch("autoim.runtime_storage._now", return_value=base + timedelta(days=50)):
            latest = backup_runtime(self.paths)
        self.assertFalse((self.paths.backups / first["id"]).exists())
        self.assertFalse((self.paths.backups / expired["id"]).exists())
        self.assertTrue((self.paths.backups / protected["id"]).exists())
        self.assertTrue((self.paths.backups / latest["id"]).exists())
        self.assertTrue(self.paths.pending_db.exists())

    def test_config_recovery_works_even_if_database_probe_is_unavailable(self):
        self.paths.initialize()
        self.config(self.paths.account_backup)
        store = VisualStore(self.paths.isolated_db)
        with patch.object(store, "transaction", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.Error):
                PersistentAccount(self.paths.account).ensure(store, lambda: None)
        self.assertEqual(CoreAccount.load(self.paths.account), ACCOUNT)

    def test_backfill_preserves_account_id_and_recovers_without_config(self):
        self.paths.initialize()
        store = VisualStore(self.paths.isolated_db)
        with store.transaction() as db:
            before = bind_core_account(db, ACCOUNT, "2026-05-01")
            db.execute("UPDATE accounts SET email=NULL WHERE id=?", (before,))
        config = PersistentAccount(self.paths.account)
        config.ensure(store, lambda: None, supplied=ACCOUNT)
        with store.transaction() as db:
            self.assertEqual(tuple(db.execute("SELECT id,email FROM accounts").fetchone()), (before, ACCOUNT.email))
        self.paths.account.unlink()
        self.paths.account_backup.unlink()
        self.assertEqual(config.ensure(store, lambda: None), (ACCOUNT, "ISOLATED_DATABASE"))

    def test_hash_verified_backup_recovers_config_when_database_is_absent(self):
        self.paths.initialize()
        self.config(self.paths.account)
        snapshot = backup_runtime(self.paths, "config")
        self.paths.account.unlink()
        self.assertEqual(len(tuple(config_backup_sources(self.paths))), 1)
        with patch("autoim.wecom.collection_pending.runtime_paths", return_value=self.paths):
            result = PersistentAccount(self.paths.account).ensure(VisualStore(self.paths.isolated_db), lambda: None)
        self.assertEqual(result, (ACCOUNT, "HISTORICAL_BACKUP"))
        self.assertEqual(CoreAccount.load(self.paths.account_backup), ACCOUNT)
        self.assertTrue((self.paths.backups / snapshot["id"]).exists())

    def test_isolated_backup_owner_conflict_stops_restoration(self):
        self.paths.initialize()
        store = VisualStore(self.paths.isolated_db)
        with store.transaction() as db:
            bind_core_account(db, ACCOUNT, "2026-05-01")
        with closing(sqlite3.connect(self.paths.isolated_db)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
        backup_runtime(self.paths)
        self.paths.isolated_db.unlink()
        self.config(self.paths.account, CoreAccount("Other", "other@example.test"))
        with self.assertRaisesRegex(RuntimeDataError, "MISSING_NO_VALID_BACKUP"):
            restore_missing(self.paths, "isolated")


class VisualBoundaryTests(TestCase):
    def test_top_complete_nonunread_row_requires_contact_structure_and_text(self):
        h, w = 810, 1200
        pixels = np.full((h, w, 3), 247, dtype=np.uint8)
        for y in (150, 276, 402, 528):
            pixels[y:y+80, 137:217] = (70, 100, 142)
        for y in (252, 378, 504, 630):
            pixels[y:y+2, 230:578] = (217, 217, 217)
        boxes = {"B1": Box(100, 0, 579, 252), "B2": Box(100, 252, 579, h), "C1": Box(579, 0, w, 123)}
        layout = LayoutObservation(tuple(UiRegion(name, b, b.relative(w, h), "automatic")
                                         for name, b in boxes.items()), "CANDIDATE")
        name = TextSpan("合成会话（示例）", Box(238, 142, 430, 179), .96)
        preview = TextSpan("合成预览", Box(238, 209, 331, 238), .96)
        clock = TextSpan("10:39", Box(505, 142, 565, 174), .96)
        valid = recover_b2_start(layout, pixels, 1.75, VisualThresholds(), read_text=lambda *_: (name, preview, clock))
        self.assertEqual(valid.get("B2").top, 123)
        invalid = recover_b2_start(layout, pixels, 1.75, VisualThresholds(), read_text=lambda *_: (name, preview))
        self.assertEqual(invalid.get("B2").top, 252)

    def test_sparse_background_candidate_is_diagnostic_only_but_body_is_kept(self):
        pixels = np.full((310, 640, 3), 247, dtype=np.uint8)
        b2 = Box(0, 0, 640, 310)
        bubble = Box(18, 90, 130, 129)
        bubble.crop(pixels)[:] = (226, 225, 224)
        fake = Box(83, 145, 104, 220)
        fake.crop(pixels)[::9, :] = (215, 214, 213)
        span = TextSpan("好", Box(28, 101, 43, 117), .98)
        result = analyze_messages(pixels, b2, (Container(bubble, False), Container(fake, False)),
                                  (span,), 1, {"left_anchor": bubble.relative(640, 310)}, VisualThresholds())
        self.assertEqual([m.text for m in result.messages], ["好"])
        self.assertEqual(result.candidate_evidence[1]["classification"], "BACKGROUND_NOISE")

    def test_top_fragment_is_viewport_limit_and_bottom_truncation_is_gap(self):
        pixels = np.full((300, 640, 3), 247, dtype=np.uint8)
        box = Box(0, 0, 640, 300)
        Box(19, 0, 122, 19).crop(pixels)[:] = 223
        first = TextSpan("上方残片", Box(23, 1, 104, 14), .54)
        missed = TextSpan("中部漏读", Box(25, 120, 109, 142), .93)
        lower = Box(19, 263, 158, 300)
        lower.crop(pixels)[:] = 223
        text = TextSpan("底部一部分", Box(31, 275, 142, 294), .95)
        result = analyze_messages(pixels, box, (Container(lower, True),), (first, missed, text), 1, {}, VisualThresholds())
        self.assertEqual(result.dispositions[0].kind, "TOP_VIEWPORT_CLIPPED")
        self.assertFalse(result.dispositions[0].possible_message)
        self.assertTrue(result.dispositions[1].possible_message)
        self.assertEqual(result.messages[0].viewport_limit, "BOTTOM_VIEWPORT_CLIPPED")


class VersionTests(TestCase):
    def test_unknown_git_information_includes_reason(self):
        with TemporaryDirectory() as folder:
            self.assertIsNone(repository_state(Path(folder))["head"])
            self.assertTrue(repository_state(Path(folder))["reason"])

    def test_clean_dirty_and_changed_head(self):
        def git(root, *args):
            result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "git-repo"
            git(None, "init", str(root))
            root = root.resolve()
            git(root, "config", "user.email", ACCOUNT.email)
            git(root, "config", "user.name", ACCOUNT.name)
            source = root / "src" / "main.py"
            source.parent.mkdir()
            source.write_text("value=1\n", encoding="utf-8")
            git(root, "add", "src")
            git(root, "commit", "-m", "first")
            clean = repository_state(root)
            self.assertTrue(clean["head"])
            self.assertFalse(clean["source_dirty"])
            source.write_text("value=2\n", encoding="utf-8")
            self.assertTrue(repository_state(root)["source_dirty"])
            git(root, "commit", "-am", "second")
            self.assertNotEqual(repository_state(root)["head"], clean["head"])

    def test_export_distinguishes_launch_snapshot_from_current_head(self):
        start = {"head": "a" * 40, "source": "git", "dirty": True, "source_dirty": True, "reason": ""}
        export = {**start, "head": "b" * 40, "source_dirty": False}
        with TemporaryDirectory() as folder, patch("autoim.wecom.collection_runtime.repository_state",
                                                    side_effect=[start, export]):
            root = Path(folder)
            runtime = CollectionRuntime(root / "logs")
            try:
                runtime.emit("test", "run", "APP", "APPLICATION_STARTED")
                dest = root / "report.zip"
                runtime.export(dest, "session", "run", {})
                with zipfile.ZipFile(dest) as archive:
                    for name in ("environment.json", "diagnostic.json", "flow-report.json"):
                        summary = json.loads(archive.read(name))
                        self.assertEqual(summary["execution"]["startup_git"], start)
                        self.assertEqual(summary["export_git"], export)
                    event = json.loads(archive.read("events.jsonl"))
                    self.assertEqual(event["execution"]["startup_git"], start)
                    html = archive.read("recognition-report.html").decode()
                    self.assertIn(start["head"], html)
                    self.assertIn(export["head"], html)
            finally:
                runtime.close()
