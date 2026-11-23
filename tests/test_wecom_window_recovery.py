from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from autoim.wecom.collection_navigation import CollectionNavigation, CollectionPermit, NavigationPaused
from autoim.wecom.collection_panel import VisualCollectionPanel
from autoim.wecom.collection_service import CollectionService
from autoim.wecom.visual_capture import CaptureError, capture_visible_window
from autoim.wecom.visual_store import VisualStore
from autoim.wecom.window_manager import WindowAmbiguousError, WindowNotFoundError
from autoim.wecom.window_state import (ProcessIdentity, WindowSample, probe_window, window_diagnostic)
from test_wecom_collection import PROFILE, view
from test_wecom_visual_capture import INFO, fixture

PROCESS = ProcessIdentity("WXWork.exe", r"C:\WeCom\WXWork.exe", 1730000000.0)
PARKED = (-32000, -32000, -29432, -30476)
PLACEMENT = (0, 3, (-1, -1), (-1, -1), (100, 100, 1000, 800))
BASE = WindowSample(True, (2, INFO.pid), "WeWorkWindow", "企业微信", True, False, True, PLACEMENT,
                    INFO.rect, (1200, 720), 96, INFO.hwnd)


def health(sample=BASE, second=None, *, target=INFO, identity=PROCESS):
    manager = Mock()
    manager.resolve_window.return_value = target
    return probe_window(manager, read=Mock(side_effect=[sample, second or sample]),
                        inspect_process=Mock(return_value=identity))


class WindowStateTests(unittest.TestCase):
    def test_maximized_foreground_reports_verified_identity(self):
        state = health()
        self.assertEqual(state.status, "WINDOW_VALID")
        self.assertIsNone(state.capture_blocker)
        self.assertTrue(state.foreground)
        self.assertFalse(state.minimized)
        self.assertEqual(state.identity[:2], (INFO.pid, INFO.hwnd))
        self.assertGreaterEqual(state.elapsed_ms, 0)
        self.assertIsNotNone(state.checked_at.tzinfo)

    def test_background_maximized_keeps_read_only_capture_ready(self):
        state = health(replace(BASE, foreground_hwnd=999))
        self.assertEqual(state.status, "WINDOW_NOT_FOREGROUND")
        self.assertIsNone(state.capture_blocker)
        self.assertFalse(state.minimized)

    def test_focus_change_between_reads_does_not_change_minimized_state(self):
        for first, final in ((INFO.hwnd, 999), (999, INFO.hwnd)):
            with self.subTest(first=first, final=final):
                state = health(replace(BASE, foreground_hwnd=first), replace(BASE, foreground_hwnd=final))
                self.assertEqual(state.status, "WINDOW_NOT_FOREGROUND")
                self.assertFalse(state.minimized)
                self.assertEqual(state.foreground, final == INFO.hwnd)

    def test_normal_minimize_requires_iconic_and_placement_agreement(self):
        sample = replace(BASE, iconic=True, foreground_hwnd=0, rect=PARKED, placement=(0, 2, *PLACEMENT[2:]))
        state = health(sample, target=replace(INFO, rect=PARKED))
        self.assertEqual(state.status, "WINDOW_MINIMIZED")
        self.assertTrue(state.minimized)
        self.assertEqual(state.capture_blocker, "WINDOW_MINIMIZED")

    def test_parked_rectangle_without_iconic_flag_is_uncertain(self):
        state = health(replace(BASE, rect=PARKED), target=replace(INFO, rect=PARKED))
        self.assertEqual(state.status, "WINDOW_STATE_UNCERTAIN")
        self.assertIsNone(state.minimized)
        self.assertIn("停放位置", state.reason)
        self.assertIn("IsIconic False", window_diagnostic(state))

    def test_dpi_virtualized_parked_rectangle_is_uncertain_when_placement_corroborates(self):
        parked = (-18286, -18286, -16819, -17415)
        sample = replace(BASE, rect=parked, placement=(0, 3, parked[:2], *PLACEMENT[3:]))
        self.assertEqual(health(sample, target=replace(INFO, rect=parked)).status, "WINDOW_STATE_UNCERTAIN")

    def test_placement_conflict_is_uncertain_in_either_direction(self):
        for state in (replace(BASE, iconic=True), replace(BASE, placement=(0, 2, *PLACEMENT[2:]))):
            with self.subTest(state=state):
                self.assertEqual(health(state).status, "WINDOW_STATE_UNCERTAIN")

    def test_rect_geometry_changes_during_sampling_are_uncertain(self):
        self.assertEqual(health(BASE, replace(BASE, rect=(10, 10, 1210, 730))).status, "WINDOW_STATE_UNCERTAIN")
        self.assertEqual(health(target=replace(INFO, rect=PARKED)).status, "WINDOW_STATE_UNCERTAIN")

    def test_window_handle_reused_by_unexpected_pid_is_uncertain(self):
        state = health(BASE, replace(BASE, owner=(3, INFO.pid + 1)))
        self.assertEqual(state.status, "WINDOW_STATE_UNCERTAIN")
        self.assertIn("PID", state.reason)

    def test_process_restart_with_reused_pid_during_probe_is_uncertain(self):
        manager = Mock()
        manager.resolve_window.return_value = INFO
        inspect = Mock(side_effect=[PROCESS, replace(PROCESS, started=PROCESS.started + 10)])
        state = probe_window(manager, read=Mock(return_value=BASE), inspect_process=inspect)
        self.assertEqual(state.status, "WINDOW_STATE_UNCERTAIN")
        self.assertIn("启动时间", state.reason)

    def test_closed_and_ambiguous_resolution_are_independent(self):
        manager = Mock()
        manager.resolve_window.side_effect = WindowNotFoundError("none")
        self.assertEqual(probe_window(manager).status, "WINDOW_CLOSED")
        manager.resolve_window.side_effect = WindowAmbiguousError("multiple")
        self.assertEqual(probe_window(manager).status, "WINDOW_STATE_UNCERTAIN")

    def test_executable_or_win32_failure_is_uncertain(self):
        for state in (health(identity=replace(PROCESS, name="other.exe")), health(replace(BASE, error="native failure"))):
            self.assertEqual(state.status, "WINDOW_STATE_UNCERTAIN")

    def test_normal_geometry_with_negative_virtual_desktop_x_is_readable(self):
        state = health()
        self.assertLess(state.last.rect[0], 0)
        self.assertIsNone(state.capture_blocker)

    def test_restored_but_not_maximized_is_not_minimized(self):
        state = health(replace(BASE, zoomed=False, placement=(0, 1, *PLACEMENT[2:])))
        self.assertEqual((state.status, state.minimized, state.capture_blocker), ("WINDOW_VALID", False, "NOT_MAXIMIZED"))


class SharedCaptureTests(unittest.TestCase):
    def test_read_only_capture_remains_possible_when_foreground_changes(self):
        render = Mock(return_value=fixture()[0].pixels)
        probe = Mock(side_effect=[health(), health(replace(BASE, foreground_hwnd=999))])
        with patch("autoim.wecom.visual_capture._client_mouse", return_value=None):
            frame = capture_visible_window(Mock(), render=render, probe=probe)
        self.assertEqual(frame.diagnostics.window_health.status, "WINDOW_NOT_FOREGROUND")
        render.assert_called_once_with(INFO.hwnd, 1200, 720)

    def test_capture_failure_is_not_window_minimized(self):
        render = Mock(side_effect=OSError("PrintWindow simulated failure"))
        with patch("autoim.wecom.visual_capture._client_mouse", return_value=None):
            with self.assertRaises(CaptureError) as failed:
                capture_visible_window(Mock(), render=render, probe=Mock(return_value=health()))
        self.assertEqual(failed.exception.code, "CAPTURE_FAILED")
        self.assertEqual(failed.exception.diagnostics.window_health.status, "WINDOW_VALID")

    def test_minimize_during_capture_discards_pixels_and_reports_fresh_evidence(self):
        minimized = health(replace(BASE, iconic=True, placement=(0, 2, *PLACEMENT[2:])))
        render = Mock(return_value=fixture()[0].pixels)
        with patch("autoim.wecom.visual_capture._client_mouse", return_value=None):
            with self.assertRaises(CaptureError) as changed:
                capture_visible_window(Mock(), render=render, probe=Mock(side_effect=[health(), minimized]))
        self.assertEqual(changed.exception.code, "WINDOW_CHANGED")
        self.assertEqual(changed.exception.diagnostics.window_health.status, "WINDOW_MINIMIZED")

    def test_navigation_blocks_background_without_triggering_capture(self):
        current = health(replace(BASE, foreground_hwnd=999))
        capture = Mock()
        navigation = CollectionNavigation(CollectionPermit(), capture=capture,
                                          idle=lambda: (1, 4000), desktop=lambda: True, window_probe=lambda: current)
        with self.assertRaises(NavigationPaused) as blocked:
            navigation.verify_view(view())
        self.assertIn("WINDOW_NOT_FOREGROUND", str(blocked.exception))
        self.assertIs(blocked.exception.health, current)
        capture.assert_not_called()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = VisualStore(Path(folder.name) / "test.sqlite3")
        self.raw = view()
        self.current = health()
        self.probe = Mock(side_effect=lambda: self.current)
        self.capture = Mock(side_effect=lambda: self.raw.frame)
        self.ocr = Mock(side_effect=lambda _: self.raw)
        self.navigator, self.permit = Mock(), Mock()
        self.service = CollectionService(PROFILE, self.store, self.ocr, capture=self.capture,
                                         navigator=self.navigator, permit=self.permit, window_probe=self.probe)
        self.service.start()
        self.addCleanup(self.service.stop)

    def test_paused_checks_only_status_then_resumes_same_handle(self):
        cold = health(replace(BASE, iconic=True, placement=(0, 2, *PLACEMENT[2:])))
        self.current = cold
        paused = self.service.tick()
        self.assertTrue(paused.waiting_for_window)
        self.current = replace(cold, checked_at=cold.checked_at + timedelta(seconds=3))
        again = self.service.tick()
        self.assertGreater(again.window_health.checked_at, paused.window_health.checked_at)
        self.assertEqual(again.window_change_at, paused.window_change_at)
        self.ocr.assert_not_called()
        self.capture.assert_not_called()
        self.current = health()
        resumed = self.service.tick()
        self.assertEqual((resumed.status, resumed.bound_hwnd), ("RUNNING", INFO.hwnd))
        self.assertFalse(resumed.waiting_for_window)
        self.assertIsNotNone(resumed.resumed_at)
        self.permit.revoke.assert_not_called()

    def test_closing_and_restarting_discovers_new_pid_and_hwnd_without_revoking(self):
        self.service.tick()
        self.current = probe_window(Mock(**{"resolve_window.side_effect": WindowNotFoundError("closed")}))
        self.assertEqual(self.service.tick().window_health.status, "WINDOW_CLOSED")
        target = replace(INFO, pid=77, hwnd=303)
        self.current = health(replace(BASE, owner=(20, 77), foreground_hwnd=303), target=target)
        self.raw = replace(self.raw, frame=replace(
            self.raw.frame, capture=replace(self.raw.frame.capture, pid=77, hwnd=303)))
        resumed = self.service.tick()
        self.assertEqual((resumed.bound_pid, resumed.bound_hwnd), (77, 303))
        self.assertIn("WINDOW_RECREATED", resumed.rebind_result)
        self.permit.revoke.assert_not_called()

    def test_same_hwnd_reused_by_new_pid_is_new_binding(self):
        self.service.tick()
        self.current = health(replace(BASE, owner=(4, 71)), target=replace(INFO, pid=71))
        self.raw = replace(self.raw, frame=replace(self.raw.frame, capture=replace(self.raw.frame.capture, pid=71)))
        resumed = self.service.tick()
        self.assertEqual((resumed.bound_pid, resumed.bound_hwnd), (71, INFO.hwnd))
        self.assertIn("WINDOW_RECREATED", resumed.rebind_result)

    def test_old_snapshot_after_preflight_is_discarded_before_ocr_or_click(self):
        target = replace(INFO, hwnd=102)
        self.current = health(replace(BASE, foreground_hwnd=102), target=target)
        result = self.service.tick()
        self.assertIn("预检与截图不一致", result.reason)
        self.ocr.assert_not_called()
        self.navigator.select.assert_not_called()

    def test_stop_during_window_check_discards_late_success(self):
        def stop_then_report():
            self.service.stop()
            return health()

        self.probe.side_effect = stop_then_report
        self.assertEqual(self.service.tick().status, "STOPPED")
        self.capture.assert_not_called()
        self.assertEqual(self.service.tick().status, "STOPPED")
        self.probe.assert_called_once()

    def test_report_copies_window_reason_time_and_binding(self):
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        panel = VisualCollectionPanel(Mock(), Mock())
        self.addCleanup(panel.close)
        self.service.tick()
        self.current = health(replace(BASE, foreground_hwnd=999))
        paused = self.service.tick()
        with patch("autoim.automation.clipboard.write_unicode_text") as write:
            panel._render(paused)
            panel.copy_controls.summary_button.click()
            report = write.call_args.args[0]
        for text in ("WINDOW_NOT_FOREGROUND", "PID 42", "HWND 0x51", "等待恢复 是",
                     "GetWindowPlacement", "GetWindowRect", "IsIconic False", "最近重新绑定", "最近窗口变化"):
            self.assertIn(text, report)
        app.processEvents()
