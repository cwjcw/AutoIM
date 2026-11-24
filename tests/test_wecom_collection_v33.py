from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from concurrent.futures import CancelledError
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import numpy as np
from PySide6.QtWidgets import QApplication

from autoim.wecom.collection_navigation import CollectionNavigation, NavigationPaused
from autoim.wecom.collection_panel import VisualCollectionPanel
from autoim.wecom.collection_profile import menu_action, unique_menu
from autoim.wecom.collection_runtime import CollectionRuntime
from autoim.wecom.collection_service import CollectionService, ScanStatus
from autoim.wecom.visual_account import LoginEvidence, _signature
from autoim.wecom.visual_layout import Box
from autoim.wecom.visual_observation import observe_frame
from autoim.wecom.visual_store import VisualStore
from autoim.wecom.window_manager import WeComWindowManager, WindowResolutionError
from test_wecom_collection import PROFILE, view
import test_wecom_collection_v32 as prior
from test_wecom_visual_capture import fixture
from test_wecom_window_recovery import health


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.runtime = CollectionRuntime(self.root / "logs", revision="synthetic")
        self.addCleanup(self.runtime.close)

    def test_report_uses_observed_completion_and_dependency(self):
        event = self.runtime.emit("test", "r", "A1", "A1_MISSING", "FAILED",
                                  details={"reason": "OCR unavailable"}, elapsed_ms=15)
        self.runtime.emit("test", "r", "B2", "READ_ONLY", "AUTO_PASS")
        stages = self.runtime.report("r")["stages"]
        self.assertEqual(stages["A1"]["events"], [event])
        self.assertEqual(stages["A1"]["duration_ms"], 15)
        self.assertEqual(stages["MENU"]["status"], "BLOCKED_BY_DEPENDENCY")
        self.assertEqual(stages["B2"]["status"], "AUTO_PASS")
        self.assertEqual(stages["STOP"]["status"], "NOT_RUN")

    def test_zip_scope_and_sensitive_details(self):
        try:
            raise ValueError("payload a@example.test")
        except ValueError as exc:
            self.runtime.emit("test", "selected", "EMAIL", "COMPARE", "FAILED", exception=exc,
                              details={"reason": "a@example.test token=synthetic"})
        self.runtime.emit("test", "other", "B2", "SCAN_FROM_OTHER_DETECTION")
        target = self.root / "bundle.zip"
        self.runtime.export(target, "detection", "selected", {"scan": 1})
        with zipfile.ZipFile(target) as bundle:
            self.assertEqual(set(bundle.namelist()), {"autoim.log", "events.jsonl", "environment.json",
                                                      "diagnostic.json", "flow-report.json", "contacts.json",
                                                      "message-observations.json", "recognition-report.html",
                                                      "evidence-manifest.json"})
            raw = bundle.read("events.jsonl").decode("utf-8")
            event = json.loads(raw)
            self.assertEqual(event["detection"], "selected")
            self.assertTrue(event["exception"]["frames"])
            self.assertNotIn("a@example.test", raw)
            self.assertNotIn("token=synthetic", raw)
            self.assertIn(event["id"], bundle.read("autoim.log").decode("utf-8"))
        with self.assertRaises(FileExistsError):
            self.runtime.export(target, "all", "selected", {})
        self.assertTrue(target.is_file())

    def test_rotation_retention_and_restart(self):
        runtime = CollectionRuntime(self.root / "rotating", max_bytes=900, backups=20, revision="synthetic")
        self.addCleanup(runtime.close)
        old = runtime.root / (date.today() - timedelta(days=31)).isoformat()
        old.mkdir(parents=True)
        (old / "events.jsonl.1").write_text("old", encoding="utf-8")
        (old / "unrelated.sqlite3").write_text("original", encoding="utf-8")
        with closing(runtime):
            for index in range(6):
                runtime.emit("test", "r", "B2", "SCAN", details={"i": index, "data": "a" * 300})
            runtime.sync()
            self.assertTrue(list(runtime.root.rglob("events.jsonl.1")))
            self.assertFalse((old / "events.jsonl.1").exists())
            self.assertTrue((old / "unrelated.sqlite3").exists())
        restarted = CollectionRuntime(runtime.root, revision="synthetic")
        self.addCleanup(restarted.close)
        restarted.emit("production", "new", "B2", "DEBUG_PROBE", level="DEBUG")
        restarted.emit("production", "new", "B2", "INFO_PROBE")
        events = list(restarted.events("all"))
        self.assertEqual([row["code"] for row in events], ["SCAN"] * 6 + ["INFO_PROBE"])

class WindowPreparationTests(unittest.TestCase):
    def setUp(self):
        self.gui = Mock(error=OSError)
        self.manager = WeComWindowManager()
        self.manager._windows_api = lambda: (self.gui, Mock())
        self.front = health()
        self.back = replace(self.front, status="WINDOW_NOT_FOREGROUND",
                            last=replace(self.front.last, foreground_hwnd=99))
        self.normal = replace(self.back, last=replace(self.back.last, zoomed=False))
        self.minimized = replace(self.normal, status="WINDOW_MINIMIZED",
                                 last=replace(self.normal.last, iconic=True))

    def prepare(self, *states, check=lambda: None):
        return self.manager.prepare_collection_window(Mock(side_effect=states), lambda action, **_: action(), check,
                                                      lambda: None)

    def test_front_window_needs_no_os_action(self):
        self.assertEqual(self.prepare(self.front), self.front)
        self.assertEqual(self.gui.mock_calls, [])

    def test_minimized_restores_maximizes_and_activates(self):
        self.assertEqual(self.prepare(self.minimized, self.minimized, self.normal,
                                      self.normal, self.back, self.back, self.front), self.front)
        self.assertEqual([call.args[1] for call in self.gui.ShowWindow.call_args_list], [9, 3])
        self.gui.SetForegroundWindow.assert_called_once_with(self.front.window.hwnd)

    def test_activation_denial_is_reported(self):
        self.gui.SetForegroundWindow.side_effect = OSError("system rejected")
        with self.assertRaisesRegex(WindowResolutionError, "Windows"):
            self.prepare(self.back, self.back, self.back)

    def test_window_replaced_before_mutation_is_rejected(self):
        changed = replace(self.normal, process=replace(self.normal.process, started=999))
        with self.assertRaises(WindowResolutionError):
            self.prepare(self.normal, changed)
        self.gui.ShowWindow.assert_not_called()

    def test_cancelled_preparation_cannot_touch_native_window(self):
        cancel = Event()
        cancel.set()
        nav = CollectionNavigation(Mock(), manager=self.manager, desktop=lambda: True)
        with self.assertRaises(CancelledError):
            nav.prepare_window(cancel)
        self.assertEqual(self.gui.mock_calls, [])


class FlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = VisualStore(self.root / "test.sqlite3")
        self.runtime = CollectionRuntime(self.root / "logs", revision="synthetic")
        self.addCleanup(self.runtime.close)
        self.raw = view(unread={1: (2, False), 2: (1, False)})
        self.reader = Mock(return_value=self.raw)
        self.nav = Mock()
        self.service = CollectionService(None, self.store, self.reader, capture=lambda: self.raw.frame,
                                         navigator=self.nav, permit=Mock(), require_email=True)
        self.service.configure_runtime(self.runtime, "test")
        self.service.start()
        self.addCleanup(self.service.stop)

    def test_skipped_identity_reads_only_and_writes_isolated_metrics(self):
        self.service.assist_identity("skip")
        state = self.service.tick()
        self.assertEqual((state.unread_marker_count, state.queued), (2, 0))
        self.nav.select.assert_not_called()
        self.assertFalse(self.store.accounts())
        run = self.service.detection_id
        events = list(self.runtime.events("detection", run))
        self.assertTrue(any(event["code"] == "ISOLATED_TEST_OBSERVATION" for event in events))
        self.assertEqual(self.runtime.report(run)["stages"]["SAVE"]["status"], "BLOCKED_BY_DEPENDENCY")

    def test_manual_profile_and_no_x_can_continue_without_more_mouse_actions(self):
        frame = self.raw.frame
        avatar = Box(10, 10, 82, 82)
        login = LoginEvidence("测试使用者", "synthetic", avatar, _signature(avatar.crop(frame.pixels)), avatar_box=avatar)
        self.reader.identify.return_value = replace(self.raw, login=login)
        self.nav.profile_start.return_value = ()
        lifetime = (41, 82, (41, 82, 1., "wxwork.exe"))
        self.service._lifetime = lambda _: lifetime
        _, _, card, spans = prior.IdentityTests.profile_fixture(self)
        card = replace(card, pixels=np.full_like(card.pixels, 245))
        self.reader.profile_text.return_value = spans
        self.service._profile_tick(frame)
        self.nav.profile_surfaces.return_value = (frame, (), ())
        for _ in range(4):
            state = self.service._profile_tick(frame)
        self.assertEqual(state.assist, "CARD")
        self.assertEqual(self.nav.profile_click.call_count, 1)
        self.service.assist_identity("manual")
        self.nav.profile_surfaces.return_value = (frame, (card,), (card.hwnd,))
        self.service._profile_tick(frame)
        later = replace(frame, captured_at=frame.captured_at + timedelta(seconds=1))
        self.nav.profile_surfaces.return_value = (later, (card,), (card.hwnd,))
        state = self.service._profile_tick(later)
        self.assertEqual(state.assist, "CLOSE")
        self.assertFalse(self.store.accounts())
        self.service.assist_identity("manual")
        self.nav.profile_surfaces.return_value = (later, (), ())
        self.assertEqual(self.service._profile_tick(later).account_state, "IDENTITY_VERIFIED")
        self.assertEqual(self.nav.profile_click.call_count, 1)
        report = self.runtime.report(self.service.detection_id)["stages"]
        self.assertEqual(report["CARD"]["status"], "MANUAL_ASSISTED")
        self.assertEqual(report["RESTORE"]["status"], "MANUAL_ASSISTED")
        self.assertNotIn("example.test", str(list(self.runtime.events("all"))))

    def test_stop_keeps_failure_and_releases_permission(self):
        self.service._failure("MENU", "MISSING_MENU", "no candidate", level="ERROR")
        self.service.stop(source="hotkey")
        state = self.service.snapshot()
        self.assertEqual((state.status, state.last_failure, state.failure_code, state.stop_reason),
                         ("STOPPED", "no candidate", "MISSING_MENU", "hotkey"))
        self.assertFalse(self.service.active)
        self.service.permit.revoke.assert_called_once()
        self.assertEqual(self.runtime.report(self.service.detection_id)["stages"]["STOP"]["status"], "AUTO_PASS")
        self.assertTrue(any(row["code"] == "DETECTION_FINISHED" for row in self.runtime.events("all")))

    def test_contacts_only_reader_does_not_enter_c2_ocr(self):
        frame, reader = fixture()
        observed = observe_frame(frame, {}, reader, contacts_only=True)
        self.assertTrue(observed.contacts)
        self.assertFalse(observed.messages)
        self.assertFalse(any(area.region.startswith(("C1", "C2", "C3")) for area in observed.ocr_areas))

    def test_local_deferral_allows_next_target_with_runtime_enabled(self):
        service = CollectionService(PROFILE, self.store, self.reader, capture=lambda: self.reader.return_value.frame,
                                    navigator=self.nav, permit=Mock())
        service.configure_runtime(self.runtime, "production")
        service.start()
        self.addCleanup(service.stop)
        def select(raw, row):
            if row == raw.contacts[1]:
                raise NavigationPaused("目标被遮挡", local=True)
            self.reader.return_value = view(2)
        self.nav.select.side_effect = select
        self.assertEqual(service.tick().status, "PAUSED")
        self.assertEqual(service.tick().status, "COMPLETED")
        self.assertEqual(self.store.browse(PROFILE)["message_count"], 3)

    def test_production_rejects_incomplete_c2_and_preserves_job(self):
        service = CollectionService(PROFILE, self.store, self.reader, capture=lambda: self.reader.return_value.frame,
                                    navigator=self.nav, permit=Mock())
        service.configure_runtime(self.runtime, "production")
        service.start()
        self.addCleanup(service.stop)
        self.nav.select.side_effect = lambda *_: setattr(
            self.reader, "return_value", replace(view(1), message_completeness="VISIBLE_MESSAGES_INCOMPLETE"))
        result = service.tick()
        self.assertEqual(result.status, "UNCERTAIN")
        history = self.store.browse(PROFILE)
        self.assertEqual(history["message_count"], 0)
        clicked_job = next(job for job in history["jobs"] if job["id"] == result.trace_job)
        self.assertTrue(clicked_job["read_risk"])
        stages = self.runtime.report(service.detection_id)["stages"]
        self.assertEqual(stages["C2"]["status"], "PARTIAL")
        self.assertEqual(stages["SAVE"]["status"], "BLOCKED_BY_DEPENDENCY")

    def test_target_retries_stop_without_stopping_readonly_polling(self):
        self.raw = view(unread={1: (1, False)})
        self.reader.return_value = self.raw
        service = CollectionService(PROFILE, self.store, self.reader, capture=lambda: self.raw.frame,
                                    navigator=self.nav, permit=Mock())
        service.configure_runtime(self.runtime, "production")
        service.start()
        self.addCleanup(service.stop)
        self.nav.select.side_effect = NavigationPaused("目标被遮挡", local=True)
        for _ in range(13):
            service.tick()
        self.assertEqual(self.nav.select.call_count, 3)
        self.assertTrue(service.active)
        self.assertEqual(self.store.browse(PROFILE)["jobs"][0]["status"], "FAILED")

    def test_explicit_start_prepares_once_and_poll_does_not_reactivate(self):
        raw = view()
        nav = CollectionNavigation(Mock(), capture=lambda: raw.frame)
        service = CollectionService(PROFILE, self.store, lambda _: raw, capture=lambda: raw.frame,
                                    navigator=nav, permit=Mock())
        service.configure_runtime(self.runtime, "test")
        with patch.object(nav, "prepare_window", return_value=health()) as prepare:
            service.start()
            self.addCleanup(service.stop)
            self.assertEqual(service.tick().status, "RUNNING")
            self.assertEqual(service.tick().status, "RUNNING")
            prepare.assert_called_once()

    def test_profile_foreground_wait_does_not_use_retry_budget(self):
        frame = self.raw.frame
        avatar = Box(10, 10, 82, 82)
        login = LoginEvidence("测试使用者", "synthetic", avatar, _signature(avatar.crop(frame.pixels)), avatar_box=avatar)
        self.reader.identify.return_value = replace(self.raw, login=login)
        self.nav.profile_start.return_value = ()
        self.service._profile_tick(frame)
        self.nav.profile_surfaces.side_effect = NavigationPaused("等待企业微信处于前台")
        for _ in range(7):
            result = self.service._profile_tick(frame)
        self.assertFalse(result.assist)
        self.assertEqual(self.service._profile_check.polls, 0)
        self.assertEqual(result.failure_code, "PROFILE_WAITING_FOR_WINDOW_OR_INPUT")


class ProfileEvidenceTests(unittest.TestCase):
    def test_one_label_requires_175_percent_avatar_match(self):
        menu, spans, _, _ = prior.IdentityTests.profile_fixture(self)
        avatar = menu.pixels[40:103, 42:104]
        evidence = {}
        action = menu_action(menu, spans[:2], "测试使用者", 168, avatar=avatar, evidence=evidence)
        self.assertIsNotNone(action, evidence)
        self.assertEqual(evidence["method"], "A1_AVATAR_AND_MENU")
        self.assertIsNone(menu_action(menu, (*spans[:2], spans[0]), "测试使用者", 168, avatar=avatar))

    def test_same_physical_menu_is_deduped_but_distinct_menu_is_not(self):
        menu, spans, _, _ = prior.IdentityTests.profile_fixture(self)
        standalone = menu_action(menu, spans, "测试使用者", 168)
        embedded = replace(standalone, surface=replace(menu, hwnd=111, geometry=()))
        far = replace(standalone, surface=replace(menu, hwnd=222))
        positions = {100: (200, 220), 111: (203, 220), 222: (400, 220)}
        self.assertEqual(unique_menu([embedded, standalone, far], lambda action: positions[action.surface.hwnd], 168),
                         [standalone, far])


class FailureUiTests(unittest.TestCase):
    def test_warning_dedup_and_error_dialog_is_nonactivating(self):
        from PySide6.QtCore import Qt
        app = QApplication.instance() or QApplication([])
        panel = VisualCollectionPanel(Mock(), Mock())
        self.addCleanup(panel.close)
        panel.enable_runtime(Mock())
        panel._notifier = Mock()
        state = ScanStatus(status="PAUSED", failure_code="WAIT", failure_at="1",
                           failure_stage="WINDOW", failure_level="WARNING", last_failure="foreground")
        panel._render(state)
        panel._handle_failure(state)
        panel._handle_failure(replace(state, failure_at="2"))
        panel._notifier.announce.assert_called_once()
        serious = replace(state, failure_code="PROFILE", failure_at="3", failure_level="ERROR", assist="CARD")
        panel._render(serious)
        panel._handle_failure(serious)
        dialog = panel._exception_dialog
        self.assertTrue(dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.addCleanup(dialog.close)
        app.processEvents()
