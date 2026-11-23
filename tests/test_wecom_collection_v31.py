"""PC-V3.1 login boundaries, committed feedback and tray notification."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from autoim.wecom.collection_events import CollectionTrace, EventTime, SaveNotice
from autoim.wecom.collection_navigation import CollectionNavigation, CollectionPermit, NavigationPaused
from autoim.wecom.collection_service import CollectionReader, CollectionService
from autoim.wecom.visual_account import LoginEvidence, read_login
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_ocr import OcrUnavailable
from autoim.wecom.visual_store import VisualStore, _MIGRATIONS
from test_wecom_visual_capture import FixtureReader, fixture, span
from test_wecom_visual_partial import _history
from test_wecom_window_recovery import BASE, INFO, PLACEMENT, health


LOGIN = "测试登录甲"
OTHER = "测试登录乙"


def login_observation(raw, name=LOGIN, seed=1, *, avatar=True):
    pixels = raw.frame.pixels.copy()
    if avatar:
        pixels[8:38, 25:60] = np.random.default_rng(seed).integers(0, 220, (30, 35, 3), dtype=np.uint8)
    frame = replace(raw.frame, capture=replace(raw.frame.capture, image=pixels))
    recognizer = FixtureReader((span(name, 12, 41),)) if name else FixtureReader(())
    return replace(raw, frame=frame, login=read_login(pixels, raw.layout.get("A"), recognizer))


def opened_a_with_unread_b(raw):
    rows = tuple(replace(row, is_selected=i == 0, has_unread_indicator=i == 1,
                         unread_count=1 if i == 1 else None, unread_count_reliable=i == 1,
                         badge=Box(130, 160, 144, 174) if i == 1 else None)
                 for i, row in enumerate(raw.contacts))
    conversation = replace(raw.conversation, title=rows[0].display_name, selected_name=rows[0].display_name)
    return replace(raw, contacts=rows, conversation=conversation)


def without_unread(raw):
    rows = tuple(replace(row, has_unread_indicator=False, unread_count=None, unread_count_reliable=False, badge=None)
                 for row in raw.contacts)
    return replace(raw, contacts=rows)


class LoginReaderTests(unittest.TestCase):
    def test_collection_reader_identifies_a1_on_a_single_instance(self):
        frame, _ = fixture()
        reader = CollectionReader(None, None)
        reader._ocr = FixtureReader((span(LOGIN, 12, 41, score=.99), span("消息", 28, 75),
                                     span("右侧联系人", 430, 40)))
        result = reader.identify(frame)
        self.assertEqual(result.login.name, LOGIN)
        self.assertEqual(result.login.box.right, result.layout.get("A").right)
        self.assertEqual(len(reader._ocr.regions), 1)
        self.assertEqual(result.contacts, ())

    def test_low_confidence_toolbar_and_conflicting_names_do_not_bind(self):
        frame, _ = fixture()
        rail = Box(0, 0, 86, 720)
        for spans in ((span(LOGIN, 12, 41, score=.5),), (span("消息", 12, 41),),
                      (span(LOGIN, 12, 25), span(OTHER, 12, 44))):
            with self.subTest(spans=spans):
                result = read_login(frame.pixels, rail, FixtureReader(spans))
                self.assertFalse(result.name)
                self.assertIn("无歧义", result.reason)

    def test_a1_ocr_failure_has_a_diagnostic(self):
        frame, _ = fixture()
        result = read_login(frame.pixels, Box(0, 0, 86, 720),
                            Mock(read=Mock(side_effect=OcrUnavailable("test"))))
        self.assertEqual(result.reason, "A1 账号 OCR 失败")

    def test_wide_175_percent_rail_uses_dpi_and_the_avatar_left_of_name(self):
        pixels = np.full((500, 820, 3), 215, dtype=np.uint8)
        pixels[51:102, 36:82] = np.random.default_rng(83).integers(0, 255, (51, 46, 3), dtype=np.uint8)
        rail = Box(0, 0, 319, 500)
        reader = FixtureReader((span(LOGIN, 96, 64, 73), span("智能助理", 96, 147), span("会话乙", 423, 43)))
        evidence = read_login(pixels, rail, reader, 168)
        self.assertEqual(evidence.name, LOGIN)
        self.assertEqual(evidence.box.bottom, 119)
        self.assertTrue(evidence.avatar)


class _ScriptedReader(CollectionReader):
    def __init__(self, owner):
        self.owner = owner

    def identify(self, frame):
        return replace(self.owner.raw, frame=frame)

    def __call__(self, frame, *, on_conversation=None):
        raw = self.identify(frame)
        if on_conversation:
            on_conversation(raw)
        return raw


class AccountCollectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data.sqlite3"
        self.store = VisualStore(self.path)
        self.raw = login_observation(without_unread(_history()[1]))
        self.navigator = Mock()
        self.permit = Mock()
        self.service = self.make_service()

    def capture(self):
        frame = replace(self.raw.frame, captured_at=datetime.now().astimezone())
        return frame

    def make_service(self, store=None):
        service = CollectionService(None, store or self.store, _ScriptedReader(self), capture=self.capture,
                                    navigator=self.navigator, permit=self.permit)
        self.addCleanup(service.stop)
        return service

    def table_count(self, table):
        with self.store.transaction() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_first_account_created_only_after_independent_observations(self):
        self.service.start()
        state = self.service.tick()
        self.assertEqual((state.account_state, state.account_id), ("IDENTIFYING", None))
        self.assertEqual(self.store.accounts(), ())
        state = self.service.tick()
        self.assertEqual((state.account_state, state.account_name), ("BOUND", LOGIN))
        self.assertEqual(self.store.accounts()[0].id, state.account_id)
        self.assertEqual(self.table_count("messages"), 0)

    def test_restart_restores_matching_visual_evidence_under_same_local_id(self):
        first = self.service.identify_account()
        self.assertTrue(self.raw.login.avatar)
        reopened = self.make_service(VisualStore(self.path)).identify_account()
        self.assertEqual((reopened.account_state, reopened.account_id), ("BOUND", first.account_id))
        self.assertEqual(len(self.store.accounts()), 1)

    def test_name_only_does_not_resolve_historical_identity_after_restart(self):
        self.raw = login_observation(_history()[1], avatar=False)
        first = self.service.identify_account()
        self.assertFalse(self.raw.login.avatar)
        second = self.make_service().identify_account()
        self.assertEqual(second.account_state, "CONFIRMATION_REQUIRED")
        self.assertEqual([row.id for row in second.account_choices], [first.account_id])

    def test_same_name_indistinguishable_history_requires_confirmation(self):
        self.store.confirm_login(self.raw.login, None)
        self.store.confirm_login(self.raw.login, None)
        state = self.service.save_current()
        self.assertEqual((state.account_state, state.account_id), ("CONFIRMATION_REQUIRED", None))
        self.assertEqual(len(state.account_choices), 2)
        self.assertEqual(self.table_count("messages"), 0)

    def test_legacy_upgrade_preserves_existing_messages_and_confirms_same_account_id(self):
        # Create a genuine v2 database with user data before opening the v3 store.
        with closing(sqlite3.connect(self.path)) as db, db:
            for version in (1, 2):
                for sql in _MIGRATIONS[version]:
                    db.execute(sql)
            db.execute("PRAGMA user_version=2")
            db.execute("INSERT INTO accounts(id,profile,first_seen) VALUES (23,'旧测试账号','2025-01-01')")
            db.execute("INSERT INTO conversations VALUES ('local',23,'peer','联系人','联系人','ONE_TO_ONE',"
                       "'VISUAL_PROVISIONAL','{}',1,'2025-01-01','2025-01-01','OBSERVED')")
            db.execute("INSERT INTO messages(id,conversation_id,position,direction,kind,body,first_seen,last_seen,"
                       "status,completeness,source,origin) VALUES ('msg','local',1,'INBOUND','TEXT','历史记录',"
                       "'2025','2025','CONFIRMED','RELIABLE','WINDOW_VISUAL_OCR','HISTORY_BASELINE')")
        state = self.service.identify_account()
        self.assertEqual((state.account_state, state.account_choices[0].id), ("CONFIRMATION_REQUIRED", 23))
        state = self.service.confirm_account(23)
        self.assertEqual((state.account_id, state.saved), (23, 1))
        self.assertEqual(self.store.browse(23, "local")["messages"][0]["body"], "历史记录")

    def test_same_hwnd_account_switch_requires_confirmation_and_isolates_history(self):
        before = self.service.save_current()
        self.raw = login_observation(_history()[1], OTHER, seed=2)
        blocked = self.service.save_current()
        self.assertEqual((blocked.account_state, blocked.notice), ("CONFIRMATION_REQUIRED", None))
        self.assertEqual(self.store.browse(before.account_id)["message_count"], 3)
        after = self.service.confirm_account(None)
        self.assertNotEqual(before.account_id, after.account_id)
        self.assertEqual(self.store.browse(after.account_id)["message_count"], 0)
        self.service.save_current()
        self.assertEqual(self.store.browse(before.account_id)["message_count"], 3)
        self.assertEqual(self.store.browse(after.account_id)["message_count"], 3)

    def test_same_name_changed_avatar_blocks_old_account(self):
        before = self.service.identify_account()
        self.raw = login_observation(_history()[1], seed=13)
        blocked = self.service.save_current()
        self.assertEqual((blocked.account_name, blocked.account_state, blocked.account_id),
                         (before.account_name, "CONFIRMATION_REQUIRED", None))
        self.assertFalse(blocked.account_choices)
        self.assertEqual(self.table_count("messages"), 0)

    def test_same_avatar_with_renamed_login_requires_explicit_reuse(self):
        first = self.service.identify_account()
        self.raw = login_observation(_history()[1], OTHER)
        reopened = self.make_service()
        state = reopened.identify_account()
        self.assertEqual(state.account_state, "CONFIRMATION_REQUIRED")
        self.assertEqual([row.id for row in state.account_choices], [first.account_id])
        confirmed = reopened.confirm_account(first.account_id)
        self.assertEqual((confirmed.account_name, confirmed.account_id), (OTHER, first.account_id))

    def test_unreadable_login_is_never_replaced_by_chat_partner(self):
        self.service.identify_account()
        self.raw = login_observation(_history()[1], "")
        state = self.service.save_current()
        self.assertEqual((state.account_state, state.account_id), ("IDENTIFICATION_FAILED", None))
        self.assertIn("A1", state.account_reason)
        self.assertEqual(self.table_count("messages"), 0)

    def test_change_after_click_preserves_job_evidence_without_writing_under_old_account(self):
        account = self.service.identify_account().account_id
        self.raw = opened_a_with_unread_b(self.raw)
        self.navigator.select.side_effect = lambda *_: setattr(self, "raw", login_observation(_history()[1], OTHER))
        self.service.start()
        state = self.service.tick()
        self.assertEqual((state.status, state.account_id, state.notice), ("PAUSED", None, None))
        self.assertEqual(self.table_count("message_observations"), 0)
        job, = self.store.browse(account)["jobs"]
        self.assertEqual((job["status"], job["read_risk"]), ("UNCERTAIN", 1))

    def test_native_navigation_rejects_changed_a1_before_scoped_input(self):
        original = self.raw
        current = login_observation(_history()[1], OTHER, seed=4)
        raw = opened_a_with_unread_b(original)
        permit = Mock(spec=CollectionPermit)
        nav = CollectionNavigation(permit, capture=lambda: current.frame)
        with patch.object(nav, "verify_view", return_value=(lambda point: point, 1)), \
                self.assertRaisesRegex(NavigationPaused, "A1"):
            nav.select(raw, raw.contacts[1])
        permit.perform.assert_not_called()

    def test_change_in_last_frame_before_manual_commit_blocks_writes(self):
        self.service.identify_account()
        old = self.raw.frame
        changed = login_observation(_history()[1], OTHER, seed=4).frame
        self.service.capture = Mock(side_effect=[old, old, changed])
        result = self.service.save_current()
        self.assertIn("A1 登录账号画面已变化", result.reason)
        self.assertEqual(self.table_count("message_observations"), 0)

    def test_a_to_b_persists_ordered_events_and_notifies_only_for_committed_new_rows(self):
        baseline = self.service.save_current()
        self.assertEqual((baseline.status, baseline.notice), ("HISTORY_BASELINE", None))
        self.raw = opened_a_with_unread_b(self.raw)
        _, appended = _history(("新消息甲", .98, 35), ("新消息乙", .98, 35), ("新消息丙", .98, 35))
        self.navigator.select.side_effect = lambda *_: setattr(self, "raw", login_observation(without_unread(appended)))
        self.service.start()
        state = self.service.tick()
        self.assertEqual((state.status, state.added, state.notice.count), ("COMPLETED", 3, 3))
        self.assertEqual(self.store.browse(baseline.account_id)["message_count"], 6)
        self.assertIsNotNone(state.last_scan)
        times = state.notice.trace
        self.assertLessEqual(times.unread_detected_at.monotonic, times.navigation_started_at.monotonic)
        self.assertLessEqual(times.navigation_started_at.monotonic, times.conversation_opened_at.monotonic)
        self.assertLessEqual(times.conversation_opened_at.monotonic, times.ocr_completed_at.monotonic)
        self.assertLessEqual(times.ocr_completed_at.monotonic, times.sqlite_committed_at.monotonic)
        job, = self.store.browse(baseline.account_id)["jobs"]
        events = json.loads(job["transitions"])
        self.assertEqual(job["id"], state.notice.job_id)
        self.assertEqual(events[-1]["status"], "SQLITE_COMMITTED")
        self.assertEqual(events[-1]["detail"]["new"], 3)
        self.assertIn("unread_to_commit_ms", events[-1]["detail"])
        self.assertNotIn("新消息甲", job["transitions"])

        from PySide6.QtWidgets import QApplication
        from autoim.wecom.collection_panel import VisualCollectionPanel

        app = QApplication.instance() or QApplication([])

        def run_worker(fn, *, on_result, **_):
            on_result(fn())

        panel = VisualCollectionPanel(run_worker, Mock())
        self.addCleanup(panel.close)
        panel.store = self.store
        panel._notifier._tray_factory = Mock()
        panel._notifier._tray_factory.isSystemTrayAvailable.return_value = False
        panel._finished((state, [job]))
        self.assertNotIn("桌面通知不可用", panel._report(False))
        panel._notifier.flush()
        self.assertIn("桌面通知", panel._report(False))
        self.assertIn("系统桌面通知不可用", panel._report(False))
        job, = self.store.browse(baseline.account_id)["jobs"]
        notification = json.loads(job["transitions"])[-1]
        self.assertEqual((notification["status"], notification["detail"]["new"]), ("DESKTOP_NOTIFICATION", 3))
        app.processEvents()

        self.service.stop()
        repeated = self.service.save_current()
        self.assertIsNone(repeated.notice)
        self.assertEqual(repeated.repeated - state.repeated, 6)

    def test_window_pause_rebinds_matching_login_and_stop_keeps_permit_revoked(self):
        bound = self.service.identify_account()
        cold = health(replace(BASE, iconic=True, placement=(0, 2, *PLACEMENT[2:])))
        new = health(replace(BASE, owner=(5, 77), foreground_hwnd=303),
                     target=replace(INFO, pid=77, hwnd=303))
        self.service._window_probe = Mock(side_effect=[health(), cold, new])
        self.service.start()
        self.assertEqual(self.service.tick().account_id, bound.account_id)
        paused = self.service.tick()
        self.assertTrue(paused.waiting_for_window)
        self.assertEqual(paused.window_health.status, "WINDOW_MINIMIZED")
        frame = self.raw.frame
        self.raw = replace(self.raw, frame=replace(frame, capture=replace(frame.capture, pid=77, hwnd=303)))
        resumed = self.service.tick()
        self.assertEqual((resumed.status, resumed.account_id, resumed.bound_hwnd), ("RUNNING", bound.account_id, 303))
        self.assertIn("WINDOW_RECREATED", resumed.rebind_result)
        self.permit.revoke.assert_not_called()
        self.service.stop()
        self.assertEqual(self.service.tick().status, "STOPPED")
        self.permit.revoke.assert_called_once()
        self.navigator.select.assert_not_called()

    def test_rolled_back_sqlite_transaction_produces_no_success_notice(self):
        base = self.service.save_current()
        self.raw = login_observation(_history(("仅出现一次", .98, 35))[1])
        with patch.object(self.store, "_checkpoint", side_effect=sqlite3.OperationalError("test rollback")):
            result = self.service.save_current()
        self.assertEqual((result.status, result.notice), ("FAILED", None))
        self.assertEqual(result.recent.sqlite_committed_at, base.recent.sqlite_committed_at)
        self.assertEqual(self.store.browse(base.account_id)["message_count"], 3)


class FeedbackTests(unittest.TestCase):
    def stamp(self, seconds, wall_shift=0):
        return EventTime(datetime(2026, 1, 2, tzinfo=timezone(timedelta(hours=8))) + timedelta(seconds=wall_shift),
                         seconds)

    def test_duration_uses_monotonic_clock_and_omits_missing_steps(self):
        trace = CollectionTrace(self.stamp(10), None, self.stamp(11.5, -3600), self.stamp(14), self.stamp(14.25))
        self.assertEqual(trace.durations(), {"unread_to_open_ms": 1500, "open_to_ocr_ms": 2500,
                                            "ocr_to_commit_ms": 250, "unread_to_commit_ms": 4250})
        self.assertNotIn("navigation_started_at", trace.detail())
        self.assertTrue(trace.detail()["sqlite_committed_at"].endswith("+08:00"))
        self.assertEqual(CollectionTrace(ocr_completed_at=self.stamp(12), sqlite_committed_at=self.stamp(11)).durations(),
                         {})

    def test_tray_batches_deduplicates_and_does_not_activate_a_window(self):
        from PySide6.QtWidgets import QApplication
        from autoim.wecom.collection_notifications import CollectionNotifier

        app = QApplication.instance() or QApplication([])
        tray = Mock()
        factory = Mock(return_value=tray)
        required = app.activeWindow()
        notifier = CollectionNotifier(tray_factory=factory)
        receipts = Mock()
        notifier.reported.connect(receipts)
        first = SaveNotice("one", "测试联系人", 3, CollectionTrace())
        second = SaveNotice("two", "另一个会话", 2, CollectionTrace())
        notifier.enqueue(first)
        notifier.enqueue(first)
        notifier.enqueue(second)
        notifier.flush()
        notifier.enqueue(first)
        notifier.flush()
        self.assertEqual([call[0] for call in tray.method_calls], ["setToolTip", "show", "showMessage"])
        self.assertIn("新增 5 条", tray.showMessage.call_args.args[1])
        self.assertNotIn("测试联系人", tray.showMessage.call_args.args[1])
        self.assertIs(app.activeWindow(), required)
        self.assertEqual(receipts.call_args.args[0], (first, second))

    def test_tray_failure_is_reported_without_retry(self):
        from PySide6.QtWidgets import QApplication
        from autoim.wecom.collection_notifications import CollectionNotifier

        app = QApplication.instance() or QApplication([])
        factory = Mock()
        factory.isSystemTrayAvailable.return_value = False
        notifier = CollectionNotifier(tray_factory=factory)
        receipts = Mock()
        notifier.reported.connect(receipts)
        notifier.enqueue(SaveNotice("one", "测试联系人", 1, CollectionTrace()))
        notifier.flush()
        self.assertIn("不可用", receipts.call_args.args[1])
        factory.assert_not_called()
        app.processEvents()

    def test_panel_reports_real_events_and_never_labels_repeated_rows_as_success(self):
        from PySide6.QtWidgets import QApplication
        from autoim.wecom.collection_panel import VisualCollectionPanel
        from autoim.wecom.collection_service import ScanStatus

        app = QApplication.instance() or QApplication([])
        panel = VisualCollectionPanel(Mock(), Mock())
        self.addCleanup(panel.close)
        notifier = Mock()
        with patch.object(panel, "_notifier", notifier):
            recent = CollectionTrace(ocr_completed_at=self.stamp(3), sqlite_committed_at=self.stamp(4))
            state = ScanStatus("SAME_VISIBLE_SEQUENCE", "稳定会话", account_name=LOGIN, account_state="BOUND",
                               account_id=27, account_at=self.stamp(1), repeated=3, recent=recent,
                               sqlite_result="SAME_VISIBLE_SEQUENCE · 新增 0 · 重复 3")
            panel._finished((state, []))
            notifier.enqueue.assert_called_once_with(None)
        self.assertIn("本地 ID 27", panel.summary.text())
        self.assertIn("最近新增保存耗时 —", panel.summary.text())
        report = panel._report(False)
        self.assertIn(LOGIN, report)
        self.assertIn("SQLite 最近成功提交 2026-01-02", report)
        self.assertIn("导航尝试 —", report)
        app.processEvents()
