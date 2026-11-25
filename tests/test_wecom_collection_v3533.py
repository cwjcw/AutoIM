import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

import cv2
import numpy as np

import test_wecom_collection_v35 as flow
from autoim.wecom.collection_core import CoreAnalysis, CoreMessage
from autoim.wecom.collection_incremental import CoreAccount, save_visible
from autoim.wecom.collection_pending import CoreDurability, PendingBatch, PendingCollections, SavingBlocked
from autoim.wecom.collection_recovery import _raster, _record
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_store import VisualStore


ACCOUNT = CoreAccount("Synthetic test account", "synthetic@example.test")
OBSERVED = datetime(2026, 7, 11, 18, tzinfo=timezone(timedelta(hours=8)))


def check():
    pass


class StorageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="autoim-storage-test-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.store = VisualStore(self.root / "isolated.sqlite3")
        self.durable = CoreDurability(self.store, self.root / "core-default-account.json",
                                      self.root / "pending" / "observations.db")
        self.queue = self.durable.queue

    def init(self):
        return self.durable.account.ensure(self.store, check, supplied=ACCOUNT)

    def batch(self, *bodies, at=OBSERVED, times=None, gaps=()):
        times = times or ["今天 10:10"] * len(bodies)
        messages = tuple(CoreMessage(f"obs-{i}", "task", "Contact B", i, "INBOUND", "TEXT", body, clock, "", .98,
                                     Box(20, i * 50, 160, i * 50 + 40), "RELIABLE", "UNKNOWN", "", "CANDIDATE")
                         for i, (body, clock) in enumerate(zip(bodies, times, strict=True), 1))
        return PendingBatch.from_observation(ACCOUNT, CoreAnalysis(messages, "SCROLLBAR_BOTTOM",
                                             "VISIBLE_MESSAGES_ACCOUNTED_FOR", gaps), "CONTACT", at,
                                             "EXACT_VISUAL_IDENTITY", "run", "task", ("c2",))

    def read(self, sql):
        with closing(sqlite3.connect(self.store.path)) as db:
            return db.execute(sql).fetchall()

    def test_first_initialization_is_atomic_and_existing_files_are_stable(self):
        account, source = self.init()
        self.assertEqual((account, source), (ACCOUNT, "SUPPLIED"))
        files = [self.durable.account.path, self.durable.account.backup]
        data = [(path.stat().st_mtime_ns, path.read_bytes()) for path in files]
        self.assertEqual(self.durable.prepare(check)[1], "PRIMARY")
        self.assertEqual(data, [(path.stat().st_mtime_ns, path.read_bytes()) for path in files])
        self.assertEqual(self.read("SELECT name FROM sqlite_master WHERE name='messages'"), [])
        self.assertEqual(self.read("SELECT name FROM sqlite_master WHERE name='core_pending_write_probe'"), [])

    def test_new_instance_restores_missing_and_invalid_primary(self):
        self.init()
        path = self.durable.account.path
        path.unlink()
        restarted = CoreDurability(self.store, path, self.queue.path)
        self.assertEqual(restarted.prepare(check)[1], "BACKUP")
        path.write_bytes(b"invalid json")
        self.assertEqual(restarted.prepare(check)[1], "BACKUP")
        self.assertEqual(CoreAccount.load(path), ACCOUNT)

    def test_valid_conflicting_account_copies_are_not_overwritten(self):
        self.init()
        primary = self.durable.account.path
        primary.write_text(json.dumps({"name": "Another account", "email": "other@example.test"}), encoding="utf-8")
        with self.assertRaisesRegex(SavingBlocked, "DEFAULT_ACCOUNT_LOCAL_COPY_CONFLICT"):
            self.durable.prepare(check)
        self.assertNotEqual(CoreAccount.load(primary), ACCOUNT)
        self.assertEqual(CoreAccount.load(self.durable.account.backup), ACCOUNT)

    def test_hashed_account_without_original_email_cannot_reconstruct_identity(self):
        self.init()
        batch = self.batch("visible")
        self.queue.stage(batch)
        self.queue.process(self.store, ACCOUNT, batch.batch_id, check)
        self.durable.account.path.unlink()
        self.durable.account.backup.unlink()
        with self.assertRaisesRegex(SavingBlocked, "DEFAULT_ACCOUNT_FILE_MISSING"):
            self.durable.prepare(check)
        self.assertEqual(self.read("SELECT count(*) FROM messages"), [(1,)])

    def test_database_restoration_requires_exact_profile_and_unique_binding(self):
        self.init()
        batch = self.batch("visible")
        self.queue.stage(batch)
        self.queue.process(self.store, ACCOUNT, batch.batch_id, check)
        self.durable.account.path.unlink()
        self.durable.account.backup.unlink()
        with self.store.transaction() as db:
            db.execute("UPDATE accounts SET email=?", (ACCOUNT.email,))
        self.assertEqual(self.durable.prepare(check)[1], "ISOLATED_DATABASE")
        with self.store.transaction() as db:
            db.execute("UPDATE accounts SET profile='CORE-LOCAL-conflicting'")
        with self.assertRaisesRegex(SavingBlocked, "DEFAULT_ACCOUNT_DATABASE_CONFLICT"):
            self.durable.prepare(check)
        self.assertEqual(CoreAccount.load(self.durable.account.path), ACCOUNT)

    def test_lock_retains_independent_outbox_and_replays_using_capture_date(self):
        self.init()
        batch = self.batch("before failure", times=["昨天 9:02"])
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as locked:
            locked.execute("BEGIN IMMEDIATE")
            self.queue.stage(batch)
            result, reason = self.queue.process(self.store, ACCOUNT, batch.batch_id, check)
            self.assertIsNone(result)
            self.assertIn(reason, ("SQLITE_BUSY", "SQLITE_LOCKED"))
            self.assertEqual(self.queue.recover(self.store, ACCOUNT, check).blocked, "PENDING_RETRY_WAIT")
            locked.rollback()
        with self.queue._transaction() as db:
            db.execute("UPDATE batches SET retry_after=NULL")
        restarted = CoreDurability(VisualStore(self.store.path), self.durable.account.path, self.queue.path)
        result = restarted.prepare(check)[2]
        self.assertEqual((result.pending, result.results[0][1].first), (0, 1))
        self.assertEqual(self.read("SELECT substr(anchor, 1, 16) FROM core_message_times"), [("2026-07-10T09:02",)])

    def test_committed_target_unacknowledged_queue_is_idempotent_in_another_process(self):
        self.init()
        batch = self.batch("committed before crash")
        self.queue.stage(batch)
        digest = sha256(batch.encode().encode()).hexdigest()
        original = save_visible(self.store, ACCOUNT, batch.analysis, "CONTACT", batch.captured_at,
                                batch.run_id, check, batch_id=batch.batch_id, batch_digest=digest)
        script = """
import sys
from pathlib import Path
from autoim.wecom.collection_pending import CoreDurability
from autoim.wecom.visual_store import VisualStore
store, primary, pending = map(Path, sys.argv[1:])
recovery = CoreDurability(VisualStore(store), primary, pending).prepare(lambda: None)[2]
print(recovery.pending, recovery.results[0][1].receipt_replayed, recovery.results[0][1].committed_at)
"""
        restarted = subprocess.check_output([sys.executable, "-c", script, str(self.store.path),
                                             str(self.durable.account.path), str(self.queue.path)],
                                            text=True, timeout=20).strip()
        self.assertEqual(restarted, f"0 True {original.committed_at}")
        self.assertEqual(self.read("SELECT COUNT(*) FROM messages"), [(1,)])

    def test_cancel_before_commit_rolls_back_and_retains_pending(self):
        self.init()
        batch = self.batch("one", "two")
        self.queue.stage(batch)
        count = 0

        def cancel_at_commit():
            nonlocal count
            count += 1
            if count == 5:
                raise CancelledError()

        with self.assertRaises(CancelledError):
            self.queue.process(self.store, ACCOUNT, batch.batch_id, cancel_at_commit)
        self.assertEqual(self.read("SELECT name FROM sqlite_master WHERE name='messages'"), [])
        recovered = self.queue.recover(self.store, ACCOUNT, check)
        self.assertEqual((recovered.results[0][1].first, recovered.pending), (2, 0))

    def test_late_repeat_matches_existing_and_new_tail_appends_in_order(self):
        self.init()
        early = self.batch("base", "same")
        later = self.batch("base", "same", "same", "new", at=OBSERVED + timedelta(minutes=1))
        for batch in (later, early):
            self.queue.stage(batch)
        recovered = self.queue.recover(self.store, ACCOUNT, check)
        self.assertEqual([(item.first, item.new, item.existing) for _, item in recovered.results],
                         [(2, 0, 0), (0, 2, 2)])
        self.assertEqual(self.read("SELECT body FROM messages ORDER BY position"),
                         [("base",), ("same",), ("same",), ("new",)])

    def test_late_old_view_does_not_move_current_tail_or_insert_earlier_history(self):
        self.init()
        current = self.batch("visible", at=OBSERVED + timedelta(days=1), times=["今天 12:02"])
        old = self.batch("old", times=["今天 11:20"], gaps=({"reason": "C2_OCR_FAILED", "box": None},))
        self.queue.stage(current)
        self.queue.recover(self.store, ACCOUNT, check)
        state = self.read("SELECT last_anchor,gap FROM core_conversation_tail")
        self.queue.stage(replace(old, historical_recovery=True))
        result = self.queue.recover(self.store, ACCOUNT, check).results[0][1]
        self.assertEqual((result.first, result.new, result.uncertain), (0, 0, 1))
        self.assertEqual(self.read("SELECT last_anchor,gap FROM core_conversation_tail"), state)
        self.assertEqual(self.read("SELECT COUNT(*) FROM messages"), [(1,)])

    def test_integrity_mismatch_quarantines_evidence(self):
        self.init()
        batch = self.batch("private sample")
        self.queue.stage(batch)
        with self.queue._transaction() as db:
            db.execute("UPDATE batches SET digest='incorrect'")
        recovery = self.queue.recover(self.store, ACCOUNT, check)
        self.assertEqual((recovery.invalid, recovery.pending, recovery.results), (1, 0, ()))
        with self.queue._transaction() as db:
            self.assertIn("private sample", db.execute("SELECT payload FROM batches").fetchone()[0])

    def test_capacity_preflight_retains_older_pending(self):
        self.init()
        limited = PendingCollections(self.queue.path, capacity=2020)
        sample = self.batch("a" * 700)
        limited.stage(sample)
        with self.assertRaisesRegex(SavingBlocked, "PENDING_CAPACITY_REACHED"):
            limited.stage(self.batch("b" * 700))
        with self.queue._transaction() as db:
            self.assertEqual(db.execute("SELECT id,status FROM batches").fetchall()[0][:], (sample.batch_id, "PENDING"))

    def test_expired_finished_metadata_is_pruned_with_pending_intact(self):
        self.init()
        old = self.batch("previous")
        self.queue.stage(old)
        self.queue.process(self.store, ACCOUNT, old.batch_id, check)
        waiting = self.batch("pending")
        self.queue.stage(waiting)
        with self.queue._transaction() as db:
            db.execute("UPDATE batches SET finished_at='2020-01-01T00:00:00+00:00' WHERE id=?", (old.batch_id,))
        self.queue.recover(self.store, ACCOUNT, check, limit=0)
        with self.queue._transaction() as db:
            self.assertEqual(db.execute("SELECT id,status FROM batches").fetchall()[0][:], (waiting.batch_id, "PENDING"))

    def test_two_workers_cannot_finalize_the_same_batch_twice(self):
        self.init()
        batch = self.batch("once")
        self.queue.stage(batch)
        barrier = Barrier(2)

        def retry():
            barrier.wait()
            return PendingCollections(self.queue.path).process(self.store, ACCOUNT, batch.batch_id, check)

        with ThreadPoolExecutor(2) as workers:
            outcomes = [workers.submit(retry), workers.submit(retry)]
            outcomes = [item.result(timeout=5) for item in outcomes]
        self.assertEqual(sum(result is not None for result, _ in outcomes), 1)
        self.assertEqual(self.read("SELECT count(*) FROM messages"), [(1,)])

    def test_wrong_account_cannot_recover_batch_and_click_is_blocked(self):
        self.init()
        batch = self.batch("private")
        self.queue.stage(batch)
        result = self.queue.recover(self.store, CoreAccount("Other", "other@example.test"), check)
        self.assertEqual((result.pending, result.blocked), (1, "PENDING_ACCOUNT_MISMATCH"))
        with self.assertRaisesRegex(SavingBlocked, "PENDING_BATCHES_REMAIN"):
            self.durable.before_click(ACCOUNT, check)

    def test_historical_image_checksum_and_event_link_are_required(self):
        ok, encoded = cv2.imencode(".png", np.ones((10, 10, 3), dtype=np.uint8))
        self.assertTrue(ok)
        identifier = "a" * 32
        raster = self.root / f"{identifier}-c2-original.png"
        raster.write_bytes(encoded.tobytes())
        record = {"id": identifier, "event": "ledger", "source_event": "ocr", "detection": "run", "task": "target",
                  "kind": "c2", "size": [12, 12], "regions": [{"kind": "c2", "box": [1, 1, 11, 11],
                    "files": {"original": {"file": raster.name, "sha256": sha256(encoded.tobytes()).hexdigest()}}}]}
        archive = Mock()
        archive.read.return_value = (record, self.root)
        event = {"id": "ledger", "detection": "run", "task": "target",
                 "details": {"evidence_id": identifier, "kind": "c2", "source_event": "ocr"}}
        evidence, folder = _record(archive, "run", event)
        self.assertEqual(_raster(evidence, folder, "c2", decode=True)[1], Box(1, 1, 11, 11))
        with self.assertRaisesRegex(SavingBlocked, "HISTORICAL_EVIDENCE_LINK_INVALID"):
            _record(archive, "run", {**event, "task": "wrong"})
        raster.write_bytes(b"changed")
        with self.assertRaisesRegex(SavingBlocked, "HISTORICAL_IMAGE_CHECKSUM_INVALID"):
            _raster(evidence, folder, "c2")


class CoreStorageFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = flow.CoreFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.store = self.fixture.isolated
        self.durable = CoreDurability(self.store, self.fixture.root / "core-default-account.json",
                                      self.fixture.root / "pending" / "queue.sqlite3")
        self.service._durability = self.durable

    @staticmethod
    def observed():
        observation = flow.sample(1, {2: (1, False)}, tone=160)
        return replace(observation, frame=replace(observation.frame, captured_at=OBSERVED))

    def test_preflight_failure_prevents_capture_and_mouse_selection(self):
        self.service.capture = Mock(side_effect=AssertionError("native capture attempted"))
        self.fixture.start()
        state = self.fixture.poll()
        self.assertEqual((state.stop_reason, state.storage_state), ("failure", "DEFAULT_ACCOUNT_FILE_MISSING"))
        self.service.capture.assert_not_called()
        self.fixture.navigator.select.assert_not_called()

    def test_mid_run_database_error_stages_body_and_blocks_next_click(self):
        self.durable.account.ensure(self.store, check, supplied=ACCOUNT)
        self.fixture.start()
        self.fixture.poll()
        with patch.object(self.store, "transaction", side_effect=OSError):
            state = self.fixture.poll(self.observed(), count=2)
            self.assertEqual((state.storage_pending, state.core.first_saved), (1, 0))
            self.assertEqual((state.core.messages[0].text, state.core.messages[0].save_status), (flow.BODY, "STAGED"))
            state = self.fixture.poll()
        self.assertEqual((state.stop_reason, state.core.clicks), ("failure", 1))
        with self.durable.queue._transaction() as db:
            db.execute("UPDATE batches SET retry_after=NULL")
        state = self.service.recover_pending()
        self.assertEqual((state.storage_pending, state.storage_recovered, state.core.clicks), (0, 1, 1))
        self.assertEqual(state.core.messages[0].save_status, "SAVED")
        self.assertFalse(self.fixture.formal.path.exists())

    def test_outbox_failure_keeps_in_memory_body_and_stops_native_navigation(self):
        self.durable.account.ensure(self.store, check, supplied=ACCOUNT)
        self.fixture.start()
        self.fixture.poll()
        with patch.object(self.durable.queue, "stage", side_effect=OSError):
            state = self.fixture.poll(self.observed(), count=2)
            self.assertEqual((state.storage_pending, state.core.first_saved), (0, 0))
            self.assertEqual((state.core.messages[0].text, state.core.messages[0].save_status), (flow.BODY, "FAILED"))
        state = self.fixture.poll()
        self.assertEqual((state.stop_reason, state.core.clicks), ("failure", 1))

    def test_user_cancellation_does_not_start_idle_recovery(self):
        with patch.object(self.durable, "prepare", side_effect=CancelledError) as prepare:
            before = self.service.snapshot()
            self.assertEqual(self.service.recover_pending(lambda: True), before)
            self.service.mode = "production"
            self.assertEqual(self.service.recover_pending(), before)
            prepare.assert_not_called()
        self.fixture.navigator.select.assert_not_called()
