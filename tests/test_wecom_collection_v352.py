from dataclasses import replace
from threading import Event
from time import perf_counter, sleep
from unittest import TestCase
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from autoim.wecom.collection_bubble import CollectionBubble
from autoim.wecom.collection_core import CoreView
from autoim.wecom.collection_navigation import CollectionNavigation, NavigationPaused
from autoim.wecom.collection_test import PHASES, TestClock, TestDeadline
from autoim.wecom.collection_worker import CoreReadWorker
from autoim.wecom.visual_contacts import recover_b2_start
from autoim.wecom.visual_layout import Box, LayoutObservation, UiRegion, VisualThresholds
from autoim.wecom.visual_observation import observe_frame
from test_wecom_collection import view
from test_wecom_visual_capture import fixture
from test_wecom_window_recovery import health


def _stall_read(connection, *_):
    with connection:
        operation = connection.recv()
        if operation == "stall":
            sleep(25)
        connection.send(("OK", operation))


class NavigationTests(TestCase):
    def test_titlebar_and_other_region_centers_are_never_native_action_points(self):
        nav = CollectionNavigation(Mock())
        point_check = Mock(side_effect=NavigationPaused("TitleBarWindow"))
        with patch.object(nav, "verify_view", return_value=(point_check, 1)) as native:
            nav.verify_normal_message(view())
        native.assert_not_called()
        point_check.assert_not_called()

    def test_actual_b2_point_with_foreign_root_is_still_refused(self):
        raw = view(unread={1: (1, False)})
        nav = CollectionNavigation(Mock(), desktop=lambda: True, idle=lambda: (5, 9000), window_probe=health)
        nav.report = Mock()
        with (patch("win32gui.IsWindowEnabled", return_value=True),
              patch("win32gui.ClientToScreen", return_value=(0, 0)),
              patch("win32gui.WindowFromPoint", return_value=999),
              patch("win32gui.GetAncestor", return_value=999),
              patch("win32gui.GetClassName", return_value="TitleBarWindow"),
              patch("win32gui.GetWindow", return_value=23),
              patch("win32process.GetWindowThreadProcessId", return_value=(7, 35)),
              patch("autoim.wecom.collection_navigation._occlusion_detail", return_value="目标被遮挡")):
            check, _ = nav.verify_view(raw)
            point = raw.contacts[1].box.center
            with self.assertRaises(NavigationPaused):
                check(point)
        facts = nav.report.call_args.kwargs["details"]
        self.assertEqual((facts["point"], facts["class_name"], facts["owner"], facts["pid"]), (point, "TitleBarWindow", 23, 35))
        self.assertTrue(facts["actual_point"] and facts["obscured"])

    def test_native_deadline_is_checked_before_moving_or_clicking(self):
        raw = view(unread={1: (1, False)})
        permit = Mock()
        permit.perform.side_effect = lambda action, **_: action()
        nav = CollectionNavigation(permit, idle=lambda: (5, 9000))
        with (patch.object(nav, "verify_view", return_value=(lambda point: point, 5)),
              patch("win32api.SetCursorPos") as move,
              patch("win32api.mouse_event") as click):
            with self.assertRaises(TestDeadline):
                nav.select(raw, raw.contacts[1], fresh_capture=lambda: raw.frame,
                           deadline_check=Mock(side_effect=TestDeadline(4, "STAGE")))
        move.assert_not_called()
        click.assert_not_called()

    def test_175_percent_highlight_seam_cannot_hide_a_badged_first_row(self):
        width, height = 1200, 810
        pixels = np.full((height, width, 3), 247, dtype=np.uint8)
        for y in (150, 276, 402, 528):
            pixels[y:y + 80, 137:217] = (70, 100, 142)
        cv2.circle(pixels, (217, 154), 15, (27, 25, 237), -1)
        boxes = {"B1": Box(100, 0, 579, 252), "B2": Box(100, 252, 579, height),
                 "C1": Box(579, 0, width, 123)}
        layout = LayoutObservation(tuple(UiRegion(key, box, box.relative(width, height), "自动分界")
                                         for key, box in boxes.items()), "CANDIDATE")
        thresholds = VisualThresholds()
        recovered = recover_b2_start(layout, pixels, 1.75, thresholds)
        self.assertEqual(recovered.get("B2").top, 127)
        manual = replace(layout, regions=tuple(replace(r, evidence="人工标定") for r in layout.regions))
        self.assertEqual(recover_b2_start(manual, pixels, 1.75, thresholds).get("B2").top, 252)

    def test_independent_read_skips_failed_b2_and_still_reads_c2(self):
        frame, reader = fixture()
        raw = observe_frame(frame, {}, reader, message_gate=lambda _: True, history_only=True)
        self.assertEqual(raw.page, "MESSAGES_CANDIDATE")
        self.assertTrue(raw.messages)
        self.assertFalse(any(area.region.startswith("B2") for area in raw.ocr_areas))


class DeadlineTests(TestCase):
    def test_limits_and_retries_use_the_original_monotonic_deadline(self):
        now = Mock(return_value=0.)
        emit = Mock()
        clock = TestClock(emit, now=now)
        self.assertEqual([limit for _, limit in PHASES.values()], [10, 15, 10, 10, 10, 20, 5])
        clock.begin(2, "识别中")
        now.return_value = 11.
        clock.progress("重新截图", "窗口变化", retry=True)
        clock.begin(2, "应保留开始时间")
        self.assertEqual(clock.snapshot()["remaining"], 4.)
        self.assertEqual(clock.phase.retries, 1)
        now.return_value = 15.
        with self.assertRaises(TestDeadline) as expired:
            clock.check()
        self.assertEqual(expired.exception.limit, "STAGE")
        self.assertTrue(clock.snapshot()["awaiting_timeout"])

    def test_contact_and_run_limits_take_precedence(self):
        now = Mock(return_value=0.)
        clock = TestClock(Mock(), now=now)
        clock.begin_target()
        now.return_value = 57.
        clock.begin(6, "C2")
        self.assertEqual(clock.snapshot()["remaining"], 3.)
        now.return_value = 60.
        with self.assertRaises(TestDeadline) as expired:
            clock.check()
        self.assertEqual(expired.exception.limit, "TARGET")
        clock.complete("TIMEOUT")
        clock.end_target()
        now.return_value = 178.
        clock.begin(7, "展示")
        self.assertEqual(clock.snapshot()["remaining"], 2.)
        now.return_value = 180.
        with self.assertRaises(TestDeadline) as expired:
            clock.check()
        self.assertEqual(expired.exception.limit, "TEST")

    def test_hung_read_process_is_killed_and_the_next_request_uses_a_fresh_process(self):
        worker = CoreReadWorker(None, None, target=_stall_read)
        self.addCleanup(worker.close)
        end = perf_counter() + 1.2

        def deadline():
            if perf_counter() >= end:
                raise TestDeadline(2, "STAGE")

        with self.assertRaises(TestDeadline):
            worker.request("stall", deadline, Event())
        self.assertIsNone(worker.process)
        self.assertEqual(worker.request("fresh", lambda: None, Event()), "fresh")

    def test_bubble_countdown_uses_phase_clock_without_completion_from_worker(self):
        app = QApplication.instance() or QApplication([])
        now = Mock(return_value=0.)
        clock = TestClock(Mock(), now=now)
        clock.begin(4, "检查点击点")
        bubble = CollectionBubble()
        self.addCleanup(bubble.close)
        self.assertTrue(bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.assertTrue(bubble.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        clock.progress("等待重新核对", "合成遮挡")
        now.return_value = 3.
        bubble.update_test(clock.snapshot(), CoreView(), "F10")
        app.processEvents()
        self.assertTrue(bubble.isVisible())
        self.assertIn("剩余 7.0", bubble.text.text())
        self.assertIn("合成遮挡", bubble.text.text())
        now.return_value = 10.
        bubble.update_test(clock.snapshot(), CoreView(), "F10")
        self.assertIn("阶段已超时", bubble.text.text())
        clock.finish("TEST_STOPPED", "用户停止")
        bubble.update_test(clock.snapshot(), CoreView(), "F10")
        self.assertIn("本轮测试结束", bubble.text.text())
